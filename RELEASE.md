# Current version (not yet released; still in development)

## Major Features and Improvements

## Bug Fixes and Other Changes
* Trim min/max value in `tft.bucketize where the computed number of bucket
  boundaries is more than requested. Updated documentation to clearly indicate
  that the number of buckets is computed using approximate algorithms, and that
  computed number can be more or less than requested.
* Change the namespace used for Beam metrics from `tensorflow_transform` to
  `tfx.Transform`.
* Update Beam metrics to also log vocabulary sizes.

## Breaking changes

## Deprecations

# Release 0.5.0

## Major Features and Improvements
* Batching of input instances is now done automatically and dynamically.
* Added analyzers to compute covariance matrices (`tft.covariance`) and
  principal components for PCA (`tft.pca`).
* CombinerSpec and combine_analyzer now accept multiple inputs/outputs.

## Bug Fixes and Other Changes
* Depends on `apache-beam[gcp]>=2.3,<3`.
* Fixes a bug where TransformDataset would not return correct output if the
  output DatasetMetadata contained deferred values (such as vocabularies).
* Added checks that the prepreprocessing function's outputs all have the same
  size in the batch dimension.
* Added `tft.apply_buckets` which takes an input tensor and a list of bucket
  boundaries, and returns bucketized data.
* `tft.bucketize` and `tft.apply_buckets` now set metadata for the output
  tensor, which means the resulting tf.Metadata for the output of these
  functions will contain min and max values based on the number of buckets,
  and also be set to categorical.
* Testing helper function assertAnalyzeAndTransformResults can now also test
  the content of vocabulary files and other assets.
* Reduces the number of beam stages needed for certain analyzers, which can be
  a performance bottleneck when transforming many features.
* Performance improvements in `tft.uniques`.
* Fix a bug in `tft.bucketize` where the bucket boundary could be same as a
  min/max value, and was getting dropped.
* Allows scaling individual components of a tensor independently with
  `tft.scale_by_min_max`, `tft.scale_to_0_1`, and `tft.scale_to_z_score`.
* Fix a bug where `apply_saved_transform` could only be applied in the global
  name scope.
* Add warning when `frequency_threshold` that are <= 1.  This is a no-op and
  generally reflects mistaking `frequency_threshold` for a relative frequency
  where in fact it is an absolute frequency.

## Breaking changes
* The interfaces of CombinerSpec and combine_analyzer have changed to allow
  for multiple inputs/outputs.
* Requires pre-installed TensorFlow >=1.5,<2.

## Deprecations

# Release 0.4.0

## Major Features and Improvements
* Added a combine_analyzer() that supports user provided combiner, conforming to
  beam.CombinFn(). This allows users to implement custom combiners
  (e.g. median), to complement analyzers (like min, max) that are
  prepackaged in TFT.
* Quantiles Analyzer (`tft.quantiles`), with a corresponding `tft.bucketize`
  mapper.

## Bug Fixes and Other Changes
* Depends on `apache-beam[gcp]>=2.2,<3`.
* Fixes some KeyError issues that appeared in certain circumstances when one
  would call AnalyzeAndTransformDataset (due to a now-fixed Apache Beam [bug]
  (https://issues.apache.org/jira/projects/BEAM/issues/BEAM-2966)).
* Allow all functions that accept and return tensors, to accept an optional
  name scope, in line with TensorFlow coding conventions.
* Update examples to construct input functions by hand instead of using helper
  functions.
* Change scale_by_min_max/scale_to_0_1 to return the average(min, max) of the
  range in case all values are identical.
* Added export of serving model to examples.
* Use "core" version of feature columns (tf.feature_column instead of
  tf.contrib) in examples.
* A few bug fixes and improvements for coders regarding Python 3.

## Breaking changes
* Requires pre-installed TensorFlow >= 1.4.
* No longer distributing a WHL file in PyPI. Only doing a source distribution
  which should however be compatible with all platforms (ie you are still able
  to `pip install tensorflow-transform` and use `requirements.txt` or `setup.py`
  files for environment setup).
* Some functions now introduce a new name scope when they did not before so the
  names of tensors may change.  This will only affect you if you directly lookup
  tensors by name in the graph produced by tf.Transform.
* Various Analyzer Specs (\_NumericCombineSpec, \_UniquesSpec, \_QuantilesSpec)
  are now private. Analyzers are accessible only via the top-level TFT functions
  (min, max, sum, size, mean, var, uniques, quantiles).

## Deprecations
* The `serving_input_fn`s on `tensorflow_transform/saved/input_fn_maker.py` will
be removed on a future version and should not be used on new code,
see the `examples` directory for details on how to migrate your code to define
their own serving functions.

# Release 0.3.1

## Major Features and Improvements
* We now provide helper methods for creating `serving_input_receiver_fn` for use
with tf.estimator.  These mirror the existing functions targeting the
legacy tf.contrib.learn.estimators-- i.e. for each `*_serving_input_fn()`
in input_fn_maker there is now also a `*_serving_input_receiver_fn()`.

## Bug Fixes and Other Changes
* Introduced `tft.apply_vocab` this allows users to separately apply a single
  vocabulary (as generated by `tft.uniques`) to several different columns.
* Provide a source distribution tar `tensorflow-transform-X.Y.Z.tar.gz`.

## Breaking Changes
* The default prefix for `tft.string_to_int` `vocab_filename` changed from
`vocab_string_to_int` to `vocab_string_to_int_uniques`. To make your pipelines
resilient to implementation details please set `vocab_filename` if you are using
the generated vocab_filename on a downstream component.

# Release 0.3.0

## Major Features and Improvements
* Added hash_strings mapper.
* Write vocabularies as asset files instead of constants in the SavedModel.

## Bug Fixes and Other Changes
* 'tft.tfidf' now adds 1 to idf values so that terms in every document in the
  corpus have a non-zero tfidf value.
* Performance and memory usage improvement when running with Beam runners that
  use multi-threaded workers.
* Performance optimizations in ExampleProtoCoder.
* Depends on `apache-beam[gcp]>=2.1.1,<3`.
* Depends on `protobuf>=3.3<4`.
* Depends on `six>=1.9,<1.11`.

## Breaking Changes
* Requires pre-installed TensorFlow >= 1.3.
* Removed `tft.map` use `tft.apply_function` instead (as needed).
* Removed `tft.tfidf_weights` use `tft.tfidf` instead.
* `beam_metadata_io.WriteMetadata` now requires a second `pipeline` argument
  (see examples).
* A Beam bug will now affect users who call AnalyzeAndTransformDataset in
  certain circumstances.  Roughly speaking, if you call `beam.Pipeline()` at
  some point (as all our examples do) you will not experience this bug.  The
  bug is characterized by an error similar to
  `KeyError: (u'AnalyzeAndTransformDataset/AnalyzeDataset/ComputeTensorValues/Extract[Maximum:0]', None)`
  This [bug](https://issues.apache.org/jira/projects/BEAM/issues/BEAM-2966) will be fixed in Beam 2.2.

# Release 0.1.10

## Major Features and Improvements
* Add json-example serving input functions to TF.Transform.
* Add variance analyzer to tf.transform.

## Bug Fixes and Other Changes
* Remove duplication in output of `tft.tfidf`.
* Ensure ngrams output dense_shape is greater than or equal to 0.
* Alters the behavior and interface of tensorflow_transform.mappers.ngrams.
* Depends on `apache-beam[gcp]=>2,<3`.
* Making TF Parallelism runner-dependent.
* Fixes issue with csv serving input function.
* Various performance and stability improvements.

## Deprecations
* `tft.map` will be removed on version 0.2.0, see the `examples` directory for
  instructions on how to use `tft.apply_function` instead (as needed).
* `tft.tfidf_weights` will be removed on version 0.2.0, use `tft.tfidf` instead.

# Release 0.1.9

## Major Features and Improvements
* Refactor internals to remove Column and Statistic classes

## Bug Fixes and Other Changes
* Remove collections from graph to avoid warnings
* Return float32 from `tfidf_weights`
* Update tensorflow_transform to use `tf.saved_model` APIs.
* Add default values on example proto coder.
* Various performance and stability improvements.
