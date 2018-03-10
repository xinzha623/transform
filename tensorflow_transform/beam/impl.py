# Copyright 2017 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""An implementation of tf.Transform using Beam.

The beam implementation takes a user defined preprocessing function (see
../api.py for how to defined a preprocessing function) and implements it as a
Beam PTransform.

The AnalyzeDataset takes the user's preprocessing function and converts into
a TensorFlow function that can be applied to each row of a dataset.  For
example if the user's preprocessing function describes normalizing a column by
subtracting its mean, the tensorflow function will contain the mean of the
column as a constant, and will subtract this value from each value of the
column.  We refer to the result of AnalyzeDataset as a "transform function".

Since AnalyzeDataset is implemented with beam, it accepts a PCollection that
represents the dataset (see below for the exact format) and returns a singleton
PCollection containing the transform function (as a serialized TF graph).

The TransformDataset PTransform takes a dataset and a transform function, and
returns the transformed dataset where the transform function is applied to each
row of the original dataset.

There is also an AnalyzeAndTransformDataset PTransform that applies
AnalyzeDataset and TransformDataset to the same input dataset, possibly with
optimizations.

Typical usage of these functions is shown below.

def preprocessing_fn(inputs):
  ...

with beam.Pipeline(...) as p:
  with beam_impl.Context(temp_dir=my_temp_dir):
    input = p | beam_impl.read_examples(..., schema)
    transformed, transform_fn = ((input, schema)
        | beam_impl.AnalyzeAndTransformDataset(preprocessing_fn))
    transformed | beam_impl.write_examples_and_metadata(
        examples_path, metadata_path)
    transform_fn | beam_impl.write_transform_fn(transform_fn_path)

Implementation note: TensorFlow code (including our code) makes frequent use of
the default graph.  We want to avoid adding to the default graph, or including
the default graph in our own SavedModel's.  This means that wherever we call
TensorFlow code (or our code that uses the default graph) we should create a
graph and mark it as the default.  This is achieved by identifying the
entrypoints into our code where this happens and creating a
"with ... .as_default()" block.  There are four places this happens.

1) In AnalyzeDatset.expand() which is typically called from the main thread
2) In _GraphState.__init__ which is called from the worker running
   _RunMetaGraphDoFn
3) In replace_tensors_with_constant_values, which is called in a beam.Map.
4) In extract_scalar_constants, which is called in a beam.Map.
"""

import collections
import datetime
import os
import threading
import uuid


import apache_beam as beam

from apache_beam.transforms import util
from apache_beam.typehints import Any
from apache_beam.typehints import Dict
from apache_beam.typehints import List
from apache_beam.typehints import Union
from apache_beam.typehints import with_input_types
from apache_beam.typehints import with_output_types

import numpy as np
import six
import tensorflow as tf
from tensorflow_transform import analyzers as tft_analyzers
from tensorflow_transform import api as tft_api
from tensorflow_transform import impl_helper
from tensorflow_transform.beam import analyzer_impls
from tensorflow_transform.beam import common
from tensorflow_transform.beam import shared
from tensorflow_transform.beam.tft_beam_io import beam_metadata_io
from tensorflow_transform.saved import saved_transform_io
from tensorflow_transform.tf_metadata import dataset_metadata
from tensorflow_transform.tf_metadata import dataset_schema

_DATASET_ELEMENT_TYPE = Dict[str, Union[common.PRIMITIVE_TYPE,
                                        # Arbitrarily-nested lists are allowed.
                                        List[Any], np.generic, np.ndarray]]



def _clear_shared_state_after_barrier(pipeline, input_barrier):
  """Clears any shared state from within a pipeline context.

  This will only be cleared once input_barrier becomes available.
  """
  empty_pcoll = input_barrier | 'MakeCheapBarrier' >> beam.FlatMap(
      lambda x: None)
  return (pipeline
          | 'PrepareToClearSharedKeepAlives' >> beam.Create([None])
          | 'WaitAndClearSharedKeepAlives' >> beam.Map(
              lambda x, empty_side_input: shared.Shared().acquire(lambda: None),
              beam.pvalue.AsIter(empty_pcoll)))


class Context(object):
  """Context manager for tensorflow-transform.

  All the attributes in this context are kept on a thread local state.

  Args:
    temp_dir: (Optional) The temporary directory used within in this block.
    desired_batch_size: (Optional) A batch size to batch elements by. If not
        provided, a batch size will be computed automatically.

  Note that the temp dir should be accessible to worker jobs, e.g. if running
  with the Cloud Dataflow runner, the temp dir should be on GCS and should have
  permissions that allow both launcher and workers to access it.

  When running on Cloud Dataflow, the temp dir should also be in a regional
  bucket, as only regional buckets provide the consistency guarantees required
  by tf.Transform.  This requirement will be removed in later versions.
  """

  _State = collections.namedtuple(  # pylint: disable=invalid-name
      '_State', ['temp_dir', 'desired_batch_size'])

  class _StateStack(object):
    """Stack of states for this context manager (found in thread-local storage).
    """

    def __init__(self):
      self.frames = []

  _TEMP_SUBDIR = 'tftransform_tmp'

  _thread_local = threading.local()

  def __init__(self, temp_dir=None, desired_batch_size=None):
    state = getattr(self._thread_local, 'state', None)
    if not state:
      self._thread_local.state = self._StateStack()
      self._thread_local.state.frames.append(self._State(None, None))

    self._temp_dir = temp_dir
    self._desired_batch_size = desired_batch_size

  def __enter__(self):
    # Previous State's properties are inherited if not explicitly specified.
    last_frame = self._get_topmost_state_frame()
    self._thread_local.state.frames.append(
        self._State(
            temp_dir=self._temp_dir
            if self._temp_dir is not None else last_frame.temp_dir,
            desired_batch_size=self._desired_batch_size
            if self._desired_batch_size is not None else
            last_frame.desired_batch_size))

  def __exit__(self, *exn_info):
    self._thread_local.state.frames.pop()

  @classmethod
  def _get_topmost_state_frame(cls):
    if cls._thread_local.state.frames:
      return cls._thread_local.state.frames[-1]
    return None

  @classmethod
  def create_base_temp_dir(cls):
    """Generate a temporary location."""
    state = cls._get_topmost_state_frame()
    if state is None or not state.temp_dir:
      raise ValueError(
          'A tf.Transform function that required a temp dir was called but no '
          'temp dir was set.  To set a temp dir use the impl.Context context '
          'manager.')
    base_temp_dir = os.path.join(state.temp_dir, cls._TEMP_SUBDIR)

    tf.gfile.MakeDirs(base_temp_dir)
    return base_temp_dir

  @classmethod
  def get_desired_batch_size(cls):
    """Retrieves a user set fixed batch size, None if not set."""
    state = cls._get_topmost_state_frame()
    if state is not None and state.desired_batch_size is not None:
      tf.logging.info('Using fixed batch size: %d' % state.desired_batch_size)
      return state.desired_batch_size
    return None


@beam.ptransform_fn
@with_input_types(_DATASET_ELEMENT_TYPE)
@with_output_types(List[_DATASET_ELEMENT_TYPE])
def _BatchElements(pcoll):  # pylint: disable=invalid-name
  """Batches elements either automatically or to the given batch_size."""
  desired_batch_size = Context.get_desired_batch_size()
  kwargs = dict(
      min_batch_size=desired_batch_size, max_batch_size=desired_batch_size
  ) if desired_batch_size is not None else {}
  return pcoll | 'BatchElements' >> util.BatchElements(**kwargs)


@with_input_types(List[_DATASET_ELEMENT_TYPE], str)
@with_output_types(Dict[str, Union[np.ndarray, tf.SparseTensorValue]])
class _RunMetaGraphDoFn(beam.DoFn):
  """Maps a PCollection of dicts to a PCollection of dicts via a TF graph.

  The TF graph may contain more inputs than the schema provided. In that case,
  a subset of the inputs will be fed, which may cause an error if the excluded
  inputs are required to produce the included outputs.

  Args:
    input_schema: A `Schema` representing the inputs of this transform phase.
    serialized_tf_config: A serialized tf.ConfigProto to use in sessions. None
      implies use Tensorflow defaults.
    shared_graph_state_handle: an instance of shared.Shared() that allows us to
      load the graph once and share it across multiple threads in the current
      process.
    exclude_outputs: (Optional) A list of names of outputs to exclude.
  """

  # Thread-safe.
  class _GraphState(object):

    def __init__(self, saved_model_dir, input_schema, exclude_outputs,
                 tf_config):
      self.saved_model_dir = saved_model_dir
      graph = tf.Graph()
      self.session = tf.Session(graph=graph, config=tf_config)
      with graph.as_default():
        with self.session.as_default():
          inputs, outputs = saved_transform_io.partially_apply_saved_transform(
              saved_model_dir, {})
        self.session.run(tf.global_variables_initializer())
        self.session.run(tf.tables_initializer())

        input_schema_keys = input_schema.column_schemas.keys()
        extra_input_keys = set(input_schema_keys).difference(inputs.keys())
        if extra_input_keys:
          raise ValueError('Input schema contained keys not in graph: %s' %
                           input_schema_keys)
        extra_output_keys = set(exclude_outputs).difference(outputs.keys())
        if extra_output_keys:
          raise ValueError('Excluded outputs contained keys not in graph: %s' %
                           exclude_outputs)
        non_excluded_output_keys = set(
            outputs.keys()).difference(exclude_outputs)
        self.inputs = {key: inputs[key] for key in input_schema_keys}
        self.outputs = {key: outputs[key] for key in non_excluded_output_keys}

  def __init__(self,
               input_schema,
               serialized_tf_config,
               shared_graph_state_handle,
               exclude_outputs=None):
    super(_RunMetaGraphDoFn, self).__init__()
    self._input_schema = input_schema
    self._exclude_outputs = (
        exclude_outputs if exclude_outputs is not None else [])
    self._serialized_tf_config = serialized_tf_config

    # The shared graph state handle allows us to load the graph once and share
    # it across multiple threads in the current process.
    self._shared_graph_state_handle = shared_graph_state_handle
    self._graph_state = None

    # Metrics.
    self._graph_load_seconds_distribution = beam.metrics.Metrics.distribution(
        common.METRICS_NAMESPACE, 'graph_load_seconds')
    self._batch_size_distribution = beam.metrics.Metrics.distribution(
        common.METRICS_NAMESPACE, 'batch_size')
    self._num_instances = beam.metrics.Metrics.counter(
        common.METRICS_NAMESPACE, 'num_instances')

  def _handle_batch(self, batch):
    self._batch_size_distribution.update(len(batch))
    self._num_instances.inc(len(batch))

    feed_dict = impl_helper.make_feed_dict(self._graph_state.inputs,
                                           self._input_schema, batch)

    try:
      return self._graph_state.session.run(
          self._graph_state.outputs, feed_dict=feed_dict)
    except Exception as e:
      tf.logging.error('%s while applying transform function for tensors %s' %
                       (e, self._graph_state.outputs))
      raise

  def _make_graph_state(self, saved_model_dir):
    start = datetime.datetime.now()
    tf_config = analyzer_impls._maybe_deserialize_tf_config(  # pylint: disable=protected-access
        self._serialized_tf_config)
    result = self._GraphState(saved_model_dir, self._input_schema,
                              self._exclude_outputs, tf_config)
    self._graph_load_seconds_distribution.update(
        int((datetime.datetime.now() - start).total_seconds()))
    return result

  def process(self, batch, saved_model_dir):
    """Runs the given graph to realize the output `Tensor` or `SparseTensor`s.

    Runs the graph in a TF session for computing the output values of the
    `Tensor` or `SparseTensor`s, given an input row of data (input `Tensor` or
    `SparseTensor`s).

    Args:
      batch: the batch of elements being processed by the DoFn
      saved_model_dir: Directory containing saved model.

    Yields:
      A representation of output features as a dict mapping keys (logical column
      names) to values.
    """
    if self._graph_state is None:
      # If available, acquire will return a cached _GraphState, since calling
      # _make_graph_state is expensive.
      self._graph_state = self._shared_graph_state_handle.acquire(
          lambda: self._make_graph_state(saved_model_dir))

    # This should remain true throughout the lifetime of this DoFn, regardless
    # of whether or not self._graph_state was cached.
    assert self._graph_state.saved_model_dir == saved_model_dir

    yield self._handle_batch(batch)


def _assert_tensorflow_version():
  # Fail with a clear error in case we are not using a compatible TF version.
  major, minor, _ = tf.__version__.split('.')
  if int(major) != 1 or int(minor) < 5:
    raise RuntimeError(
        'TensorFlow version >= 1.5, < 2 is required. Found (%s). Please '
        'install the latest 1.x version from '
        'https://github.com/tensorflow/tensorflow. ' % tf.__version__)


def _make_unique_temp_dir(base_temp_dir):
  """Create path to a unique temp dir from given base temp dir."""
  return os.path.join(base_temp_dir, uuid.uuid4().hex)


def _write_saved_transform(graph, inputs, outputs, saved_model_dir):
  """Write the given function as a saved transform."""
  with tf.Session(graph=graph) as session:
    # Remove collections that can't be serialized, as these produce annoying
    # warnings.
    # pylint: disable=protected-access
    collections_blacklist = [
        tft_api._TF_METADATA_TENSORS_COLLECTION,
        tft_api._TF_METADATA_COLUMN_SCHEMAS_COLLECTION,
        tft_api.FUNCTION_APPLICATION_COLLECTION,
        tft_analyzers.ANALYZER_COLLECTION
    ]
    # pylint: enable=protected-access
    removed_collections = []
    for collection_name in collections_blacklist:
      removed_collections.append((collection_name,
                                  graph.get_collection(collection_name)))
      graph.clear_collection(collection_name)
    # Initialize all variables so they can be saved.
    session.run(tf.global_variables_initializer())
    saved_transform_io.write_saved_transform_from_session(
        session, inputs, outputs, saved_model_dir)
    for collection_name, collection in removed_collections:
      graph.get_collection_ref(collection_name).extend(collection)


# An object used to construct a constant tensor in the graph, that will replace
# a placeholder tensor.  `value` is a numpy array.  `is_asset_filename` is set
# to true when the tensor contains an asset filename.  This information is
# needed as asset filenames must get added to a special collection when they
# are inserted into the graph, so that their names get updated when the
# SavedModel is copied.
_TensorValue = collections.namedtuple('_TensorValue',
                                      ['value', 'is_asset_filename'])


class _ReplaceTensorsWithConstants(beam.PTransform):
  """Writes a SavedModel with specified tensors replaced by constants.

  This transform takes a SavedModel in its constructor, and in its expand()
  method accepts a mapping from tensors to PCollections.  When run, it replaces
  the tensors corresponding to the keys of this mapping, with the values
  wrapped in the PCollections.

  Args:
    saved_model_dir: The directory containing the SavedModel.
  """

  def __init__(self, saved_model_dir, base_temp_dir, pipeline):
    self._saved_model_dir = saved_model_dir
    self._base_temp_dir = base_temp_dir
    # Generally the pipeline is inferred from its inputs, however we need
    # to know the pipeline for beam.Create.
    self.pipeline = pipeline

  def expand(self, tensor_pcoll_mapping):
    """Converts a dict of statistics to a transform function.

    Args:
      tensor_pcoll_mapping: A dictionary mapping `Tensor`s to a singleton
          PCollection containing a _TensorValue.

    Returns:
      A single-element PCollection containing the directory name with the
          SavedModel.
    """
    transform_fn = (
        self.pipeline
        | 'CreateTransformFn' >> beam.Create([self._saved_model_dir]))

    if not tensor_pcoll_mapping:
      return transform_fn

    # Convert tensor_value_mapping into a DictPCollectionView so it can be
    # passed as a side input to the beam Map below.
    tensor_value_pairs = []
    for name, pcoll in six.iteritems(tensor_pcoll_mapping):
      tensor_value_pairs.append(
          pcoll
          | 'AddName[%s]' % name >> beam.Map(lambda x, name=name: (name, x)))
    tensor_value_mapping = beam.pvalue.AsDict(
        tensor_value_pairs | 'MergeTensorValuePairs' >> beam.Flatten())

    def replace_tensors_with_constant_values(saved_model_dir,
                                             tensor_value_mapping):
      """Replaces specified `Tensor`s with constant values.

      Constants are accepted as Python values; these are automatically
      wrapped in `tf.constant()`.

      This method creates its own temp dir, and is therefore idempotent
      since any retry will use a different temp dir.

      Args:
        saved_model_dir: A SavedModel directory providing a transform
          graph.  The MetaGraphDef and signature are selected from the
          SavedModel using keys defined in `../constants.py` ('transform'
          and 'transform_signature', respectively).
        tensor_value_mapping: a dict of tensor names to values to use in
          place of those tensors.

      Returns:
        The directory name containing the updated SavedModel.

      Raises:
        RuntimeError: if there is no default graph available to which to
          apply the transform.
      """

      graph = tf.Graph()
      with graph.as_default():
        tensor_replacement_map = {}
        for orig_tensor_name, (value,
                               is_asset) in six.iteritems(tensor_value_mapping):
          new_tensor = tf.constant(value)
          if is_asset:
            # Any newly frozen constant tensors containing filenames must be
            # added to the ASSET_FILENAMES collection.
            graph.add_to_collection(tf.GraphKeys.ASSET_FILEPATHS, new_tensor)
          tensor_replacement_map[orig_tensor_name] = new_tensor

        with tf.Session(graph=graph) as session:
          temp_dir = _make_unique_temp_dir(self._base_temp_dir)
          input_tensors, output_tensors = (
              saved_transform_io.partially_apply_saved_transform(
                  saved_model_dir, {}, tensor_replacement_map))
          session.run(tf.global_variables_initializer())
          saved_transform_io.write_saved_transform_from_session(
              session, input_tensors, output_tensors, temp_dir)
        return temp_dir

    return (transform_fn | 'ReplaceTensorsWithConstantValues' >> beam.Map(
        replace_tensors_with_constant_values,
        tensor_value_mapping=tensor_value_mapping))


class _ComputeAnalyzerOutputs(beam.PTransform):
  """Runs analyzers and return a map from tensor names to values.

  This transform takes a PCollection of instances where each instance contains
  the inputs to the analyzers (by tensor name).  It extracts the inputs of
  each analyzer and then runs the analyzers.  The return value is a dictionary
  from the output tensor names to `_TensorValue`s.
  """

  def __init__(self, analyzers, base_temp_dir):
    self._analyzers = analyzers
    self._base_temp_dir = base_temp_dir

  def expand(self, analyzer_input_values):

    def extract_and_wrap_as_tensor_value(outputs, index, numpy_dtype, is_asset):
      return _TensorValue(np.asarray(outputs[index], numpy_dtype), is_asset)

    # For each analyzer output, look up its input values (by tensor name)
    # and run the analyzer on these values.
    result = {}
    for analyzer in self._analyzers:
      temp_assets_dir = _make_unique_temp_dir(self._base_temp_dir)
      tf.gfile.MkDir(temp_assets_dir)
      outputs_pcoll = (
          analyzer_input_values
          | 'ExtractInputs[%s]' % analyzer.name >> beam.Map(
              lambda batch, keys: [batch[key] for key in keys],
              keys=[tensor.name for tensor in analyzer.inputs])
          | 'Analyze[%s]' % analyzer.name >> analyzer_impls._AnalyzerImpl(
              analyzer.spec, temp_assets_dir))
      # pylint: enable=protected-access

      for index, tensor in enumerate(analyzer.outputs):
        is_asset = analyzer.output_is_asset(tensor)
        wrapped_output = outputs_pcoll | (
            'ExtractAndWrapAsTensorValue[%s][%d]' % (analyzer.name, index) >>
            beam.Map(extract_and_wrap_as_tensor_value, index,
                     tensor.dtype.as_numpy_dtype, is_asset))
        result[tensor.name] = wrapped_output
    return result


class _ComputeTensorValues(beam.PTransform):
  """Extracts values of tensors from a transform function.

  This transform takes the path to a SavedModel in its constructor, and in its
  expand() method accepts a mapping from tensors to PCollections.  When run, it
  replaces the tensors corresponding to the keys of this mapping, with the
  values wrapped in the PCollections.  It then extracts the values of some
  tensors in the new graph.  This allows us to compute values that depend on
  values in the tensor-PCollection mapping in arbitrary ways, where the values
  are represented by tensors in the graph that depend on the tensor-PCollection
  mapping (but not on the inputs to the graph).

  Args:
    tensor_names: The names of the tensors to extract.
    saved_model_dir: The model to extract the constants from.
    pipeline: The beam Pipeline.
  """

  def __init__(self, tensor_names, saved_model_dir, pipeline):
    self._tensor_names = tensor_names
    self._saved_model_dir = saved_model_dir
    # Generally the pipeline is inferred from its inputs, however we need
    # to know the pipeline for beam.Create.
    self.pipeline = pipeline

  def expand(self, tensor_pcoll_mapping):
    """Converts a dict of statistics to a transform function.

    Args:
      tensor_pcoll_mapping: A dictionary mapping `Tensor`s to a singleton
          PCollection containing a _TensorValue.

    Returns:
      A dict from tensor names to singleton `PCollection`s.
    """
    # Create a singleton PCollection containing self._tensor_names.  Note that
    # the PCollection is a singeton containing a single list, that is the size
    # of the PCollection is not equal to the length of self._tensor_names.
    tensor_names_pcoll = (
        self.pipeline
        | 'CreateTensorNames' >> beam.Create([self._tensor_names]))

    # Convert tensor_value_mapping into a DictPCollectionView so it can be
    # passed as a side input to the beam Map below.
    tensor_value_pairs = []
    for name, pcoll in six.iteritems(tensor_pcoll_mapping):
      tensor_value_pairs.append(
          pcoll
          | 'AddName[%s]' % name >> beam.Map(lambda x, name=name: (name, x)))
    tensor_value_mapping = beam.pvalue.AsDict(
        tensor_value_pairs
        | 'MergeTensorValuePairs' >> beam.Flatten(pipeline=self.pipeline))

    def extract_scalar_constants(tensor_names, saved_model_dir,
                                 tensor_value_mapping):
      """Extracts constant values from graph."""
      graph = tf.Graph()
      with graph.as_default():
        tensor_replacement_map = {}
        for orig_tensor_name, (value,
                               is_asset) in six.iteritems(tensor_value_mapping):
          new_tensor = tf.constant(value)
          if is_asset:
            # Any newly frozen constant tensors containing filenames must be
            # added to the ASSET_FILENAMES collection.
            graph.add_to_collection(tf.GraphKeys.ASSET_FILEPATHS, new_tensor)
          tensor_replacement_map[orig_tensor_name] = new_tensor

        with tf.Session(graph=graph) as session:
          tensor_output_map = (
              saved_transform_io.fetch_tensor_values(
                  saved_model_dir, tensor_replacement_map, tensor_names))
          session.run(tf.global_variables_initializer())
          session.run(tf.tables_initializer())
          return session.run(tensor_output_map)

    tensor_values_by_name_pcoll = (
        tensor_names_pcoll | 'ExtractScalarConstants' >> beam.Map(
            extract_scalar_constants,
            saved_model_dir=self._saved_model_dir,
            tensor_value_mapping=tensor_value_mapping))
    result = {}
    for name in self._tensor_names:
      result[name] = (
          tensor_values_by_name_pcoll
          | 'Extract[%s]' % name >> beam.Map(lambda d, name=name: d[name]))

    return result


class AnalyzeDataset(beam.PTransform):
  """Takes a preprocessing_fn and computes the relevant statistics.

  AnalyzeDataset accepts a preprocessing_fn in its constructor.  When its
  `expand` method is called on a dataset, it computes all the relevant
  statistics required to run the transformation described by the
  preprocessing_fn, and returns a TransformFn representing the application of
  the preprocessing_fn.

  Args:
    preprocessing_fn: A function that accepts and returns a dictionary from
      strings to `Tensor` or `SparseTensor`s.
  """

  def __init__(self, preprocessing_fn):
    self._preprocessing_fn = preprocessing_fn
    _assert_tensorflow_version()

  def _extract_input_pvalues(self, dataset):
    data, metadata = dataset
    pvalues = [data] + getattr(metadata, 'pcollections', {}).values()
    return dataset, pvalues

  def expand(self, dataset):
    """Analyze the dataset.

    Args:
      dataset: A dataset.

    Returns:
      A TransformFn containing the deferred transform function.

    Raises:
      ValueError: If preprocessing_fn has no outputs.
    """
    input_values, input_metadata = dataset
    input_schema = input_metadata.schema

    base_temp_dir = Context.create_base_temp_dir()

    graph = tf.Graph()
    with graph.as_default():

      with tf.name_scope('inputs'):
        inputs = input_schema.as_batched_placeholders()
      # In order to avoid a bug where import_graph_def fails when the input_map
      # and return_elements of an imported graph are the same (b/34288791), we
      # avoid using the placeholder of an input column as an output of a graph.
      # We do this by applying tf.identity to all inputs of the
      # preprocessing_fn.  Note this applies at the level of raw tensors.
      outputs = self._preprocessing_fn(impl_helper.copy_tensors(inputs))

      # At this point we check that the preprocessing_fn has at least one
      # output. This is because if we allowed the output of preprocessing_fn to
      # be empty, we wouldn't be able to determine how many instances to
      # "unbatch" the output into.
      if not outputs:
        raise ValueError('The preprocessing function returned an empty dict')

      if graph.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES):
        raise ValueError(
            'The preprocessing function contained trainable variables '
            '{}'.format(
                graph.get_collection_ref(tf.GraphKeys.TRAINABLE_VARIABLES)))

      # NOTE: it's important that create_phases is called directly after
      # preprocessing_fn, because we later mutate the graph's TABLE_INITIALIZERS
      # collection which would break the logic in create_phases.
      phases = impl_helper.create_phases()

      # Iterate through levels.  tensor_pcoll_mapping is a mapping from tensor
      # names to singleton PCollections containing a _TensorValue.  We compute
      # tensor_pcoll_mapping in phases, where at each phase we compute the
      # analyzers that are ready to run and update tensor_pcoll_mapping.
      tensor_pcoll_mapping = {}
      table_initializers = graph.get_collection_ref(
          tf.GraphKeys.TABLE_INITIALIZERS)
      original_table_initializers = list(table_initializers)
      del table_initializers[:]

      serialized_tf_config = (
          analyzer_impls._DEFAULT_TENSORFLOW_CONFIG_BY_RUNNER.get(  # pylint: disable=protected-access
              input_values.pipeline.runner))
      for level, phase in enumerate(phases):
        # Create a SavedModel that describes the mapping from the input data
        # to the inputs of the analyzers at this level.  The colum names of the
        # outputs are the tensor names of the analyzer inputs in the graph.
        # This graph has the anaylzer outputs computed so far replaced with
        # constants.
        analyzer_inputs = {}
        for analyzer in phase.analyzers:
          for input_tensor in analyzer.inputs:
            analyzer_inputs[input_tensor.name] = input_tensor
        table_initializers.extend(phase.table_initializers)
        unbound_saved_model_dir = _make_unique_temp_dir(base_temp_dir)
        _write_saved_transform(graph, inputs, analyzer_inputs,
                               unbound_saved_model_dir)
        saved_model_dir = (
            tensor_pcoll_mapping
            | 'CreateSavedModelForAnalyzerInputs[%d]' % level >>
            _ReplaceTensorsWithConstants(unbound_saved_model_dir, base_temp_dir,
                                         input_values.pipeline))

        # Run this saved model on the input dataset to obtain the inputs to the
        # analyzers.
        analyzer_input_values = (
            input_values
            | 'BatchAnalyzerInputs[%d]' % level >> _BatchElements()
            | 'ComputeAnalyzerInputs[%d]' % level >> beam.ParDo(
                _RunMetaGraphDoFn(
                    input_schema,
                    serialized_tf_config,
                    shared_graph_state_handle=shared.Shared()),
                saved_model_dir=beam.pvalue.AsSingleton(saved_model_dir)))

        # Compute the analyzers from their inputs.  `analyzer_outputs_dict` is a
        # map from tensor names to singleton PCollections of `_TensorValue`s.
        analyzer_outputs_dict = (
            analyzer_input_values
            | 'ComputeAnalyzerOutputs[%d]' % level >> _ComputeAnalyzerOutputs(
                phase.analyzers, base_temp_dir))

        # Update the mapping for all analyzers.
        tensor_pcoll_mapping.update(analyzer_outputs_dict)

      del table_initializers[:]
      table_initializers.extend(original_table_initializers)
      saved_model_dir = _make_unique_temp_dir(base_temp_dir)
      _write_saved_transform(graph, inputs, outputs, saved_model_dir)
      transform_fn = (
          tensor_pcoll_mapping
          | 'ReplaceTensorsWithConstants' >> _ReplaceTensorsWithConstants(
              saved_model_dir, base_temp_dir, input_values.pipeline))

      # Infer metadata.  The metadata may contain Futures that refer to the
      # values of tensors in the graph.  In that case, the tensors must be
      # "constant" in that they don't depend on input data.  The tensors can
      # depend on analyzer outputs though.  This allows us to set metadata that
      # depends on analyzer outputs.
      #
      # We first extract the names of the tensors that are referenced by the
      # Futures, and then compute them by calling _ComputeScalarConstants with
      # the tensor-PCollection mapping representing the analyzer outputs.
      metadata = dataset_metadata.DatasetMetadata(
          schema=impl_helper.infer_feature_schema(outputs))

      deferred_metadata_tensor_names = {
          future.name
          for column_schema in metadata.schema.column_schemas.values()
          for future in column_schema.substitute_futures({})
      }
      name_pcoll_dict = (
          tensor_pcoll_mapping
          | 'ComputeTensorValues' >>
          _ComputeTensorValues(deferred_metadata_tensor_names, saved_model_dir,
                               input_values.pipeline))
      full_metadata = beam_metadata_io.BeamDatasetMetadata(
          metadata, name_pcoll_dict)

      _clear_shared_state_after_barrier(input_values.pipeline, transform_fn)

      return transform_fn, full_metadata


class AnalyzeAndTransformDataset(beam.PTransform):
  """Combination of AnalyzeDataset and TransformDataset.

  transformed, transform_fn = AnalyzeAndTransformDataset(
      preprocessing_fn).expand(dataset)

  should be equivalent to

  transform_fn = AnalyzeDataset(preprocessing_fn).expand(dataset)
  transformed = TransformDataset().expand((dataset, transform_fn))

  but may be more efficient since it avoids multiple passes over the data.

  Args:
    preprocessing_fn: A function that accepts and returns a dictionary from
        strings to `Tensor` or `SparseTensor`s.
  """

  def __init__(self, preprocessing_fn):
    self._preprocessing_fn = preprocessing_fn
    _assert_tensorflow_version()

  def _extract_input_pvalues(self, dataset):
    data, metadata = dataset
    pvalues = [data] + getattr(metadata, 'pcollections', {}).values()
    return dataset, pvalues

  def expand(self, dataset):
    """Transform the dataset by applying the preprocessing_fn.

    Args:
      dataset: A dataset.

    Returns:
      A (Dataset, TransformFn) pair containing the preprocessed dataset and
      the graph that maps the input to the output data.
    """
    # Expand is currently implemented by composing AnalyzeDataset and
    # TransformDataset.  Future versions however could do somthing more optimal,
    # e.g. caching the values of expensive computations done in AnalyzeDataset.
    transform_fn = (
        dataset | 'AnalyzeDataset' >> AnalyzeDataset(self._preprocessing_fn))
    transformed_dataset = ((dataset, transform_fn)
                           | 'TransformDataset' >> TransformDataset())
    return transformed_dataset, transform_fn


class TransformDataset(beam.PTransform):
  """Applies the transformation computed by transforming a Dataset.

  TransformDataset's `expand` method is called on a (dataset, transform_fn)
  pair. It applies the transform_fn to each row of the input dataset and
  returns the resulting dataset.

  args:
    exclude_outputs: (Optional) Output features that should not be produced.
  """

  def __init__(self, exclude_outputs=None):
    self._exclude_outputs = exclude_outputs
    _assert_tensorflow_version()

  def _extract_input_pvalues(self, dataset_and_transform_fn):
    (data, input_metadata), (transform_fn, output_metadata) = (
        dataset_and_transform_fn)
    pvalues = ([data, transform_fn] +
               getattr(input_metadata, 'pcollections', {}).values() +
               getattr(output_metadata, 'pcollections', {}).values())
    return dataset_and_transform_fn, pvalues

  def expand(self, dataset_and_transform_fn):
    """Transforms the dataset using the transform_fn.

    Args:
      dataset_and_transform_fn: A tuple of dataset and preprocessing
      function.

    Returns:
      A dataset transformed according to the transform_fn.
    """
    (input_values, input_metadata), (transform_fn, output_metadata) = (
        dataset_and_transform_fn)

    # If exclude_outputs is set, update the output metadata.
    if self._exclude_outputs is not None:
      if isinstance(output_metadata, beam_metadata_io.BeamDatasetMetadata):
        # Unwrap BeamDatasetMetadata into DatasetMetadata and pcollections dict.
        output_metadata, pcollections = output_metadata
        schema = output_metadata.schema
        # Update DatasetMetadata to remove excluded outputs
        output_metadata = dataset_metadata.DatasetMetadata(
            schema=dataset_schema.Schema({
                key: column_schema
                for key, column_schema in six.iteritems(schema.column_schemas)
                if key not in self._exclude_outputs
            }))
        # Update pcollections to keep only pcollections that resolve futures in
        # the updated metadata.
        unresolved_future_names = set(
            future.name for future in output_metadata.substitute_futures({}))
        pcollections = {
            name: pcollection
            for name, pcollection in six.iteritems(pcollections)
            if name in unresolved_future_names
        }
        # Wrap DatasetMetadata and pcollections as BeamDatasetMetadata
        output_metadata = beam_metadata_io.BeamDatasetMetadata(
            output_metadata, pcollections)
      else:
        schema = output_metadata.schema
        output_metadata = dataset_metadata.DatasetMetadata(
            schema=dataset_schema.Schema({
                key: column_schema
                for key, column_schema in six.iteritems(schema.column_schemas)
                if key not in self._exclude_outputs
            }))

    def convert_and_unbatch(batch_dict):
      return impl_helper.to_instance_dicts(output_metadata.schema, batch_dict)

    serialized_tf_config = (
        analyzer_impls._DEFAULT_TENSORFLOW_CONFIG_BY_RUNNER.get(  # pylint: disable=protected-access
            self.pipeline.runner))
    output_instances = (
        input_values
        | 'Batch' >> _BatchElements()
        | 'Transform' >> beam.ParDo(
            _RunMetaGraphDoFn(
                input_metadata.schema,
                serialized_tf_config,
                shared_graph_state_handle=shared.Shared(),
                exclude_outputs=self._exclude_outputs),
            saved_model_dir=beam.pvalue.AsSingleton(transform_fn))
        | 'ConvertAndUnbatch' >> beam.FlatMap(convert_and_unbatch))

    _clear_shared_state_after_barrier(self.pipeline, output_instances)

    return (output_instances, output_metadata)
