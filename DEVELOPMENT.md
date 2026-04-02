# Development documentation

This documentation provides guidance on developer workflows for working with the code in this repository.

Table of Contents:
- [Development documentation](#development-documentation)
  - [Development Environment Setup](#development-environment-setup)
  - [The Development Loop](#the-development-loop)
  - [Documentation](#documentation)
    - [Code Organization](#code-organization)
  - [Testing](#testing)
    - [Writing Tests](#writing-tests)
    - [Unit Tests](#unit-tests)
      - [Running Unit Tests](#running-unit-tests)
      - [Running Docker-based Unit Tests](#running-docker-based-unit-tests)
    - [Integration Tests](#integration-tests)
      - [Running Integration Tests](#running-integration-tests)
  - [Changelog Guidelines](#changelog-guidelines)
  - [Things to Know](#things-to-know)
    - [Public Contracts](#public-contracts)
      - [Private Modules](#private-modules)
      - [Public Modules](#public-modules)
      - [On `import os as _os`](#on-import-os-as-_os)
    - [Library Dependencies](#library-dependencies)
      - [Why is a new dependency needed?](#why-is-a-new-dependency-needed)
      - [Quality of the dependency](#quality-of-the-dependency)
      - [Version Pinning](#version-pinning)
      - [Licensing](#licensing)
- [Profiling in Deadline Cloud](#profiling-in-deadline-cloud)

## Development Environment Setup

To develop the Python code in this repository you will need:

1. Python 3.8 or higher.
2. The [hatch](https://github.com/pypa/hatch) package installed (`pip install --upgrade hatch`) into your Python environment.

You can develop on a Linux, MacOS, or Windows workstation, but you may find that some of the support scripting is specific to
Linux/MacOS workstations.

If you are making changes to the Job Attachments files, then you will also need the following to be able to run the integration
tests:

1. A valid AWS Account
2. An AWS Deadline Cloud Farm and Queue.
   *  You can create these via AWS Deadline Cloud's AWS Console quick Farm create workflow.
      The Queue's configuration must include a Job Attachments bucket. If used only for running these tests then the cost of
      this infrastructure should be negligible, but do keep an eye on your costs and destroy the infrastructure (especially S3 buckets)
      when you no longer need it.

## The Development Loop

We have configured [hatch](https://github.com/pypa/hatch) commands to support a standard development loop. You can run the following
from any directory of this repository:

* `hatch build` - To build the installable Python wheel and sdist packages into the `dist/` directory.
* `hatch run test` - To run the PyTest unit tests found in the `test/unit` directory. See [Testing](#testing).
* `hatch run all:test` - To run the PyTest unit tests against all available supported versions of Python.
* `hatch run integ:test` - To run the PyTest integration tests found in the `test/integ` directory. See [Testing](#testing).
* `hatch run lint` - To check that the package's formatting adheres to our standards.
* `hatch run fmt` - To automatically reformat all code to adhere to our formatting standards.
* `hatch shell` - Enter a shell environment where you can run the `deadline` command-line directly as it is implemented in your
  checked-out local git repository.
* `hatch env prune` - Delete all of your isolated workspace [environments](https://hatch.pypa.io/1.12/environment/)
   for this package.

If you are not sure about how to approach development for this package, then we suggest a development
process along the lines of the following as a starting point:

1. Make your functional changes and make sure that they work.
2. Add unit tests for your changes and ensure that all unit tests pass.
   Iteratively improve your implementation until all unit tests pass. (See [Unit tests](#unit-tests))
3. Add integration tests for your changes if applicable. Ensure that all integration tests pass.
   Iteratively improve your implementation until all integration and unit tests pass. (See [Integration tests](#integration-tests))

Once you are satisfied with your code, and all relevant tests pass, then run `hatch run fmt` to fix up the formatting of
your code and post your pull request.

Note: Hatch uses [environments](https://hatch.pypa.io/1.16/environment/) to isolate the Python development workspace
for this package from your system or virtual environment Python. If your build/test run is not making sense, then
sometimes pruning (`hatch env prune`) all of these environments for the package can fix the issue.

## Documentation

Work-in-progress documentation for the Deadline Cloud job attachments is in progress in the [docs](docs/index.html) directory.
Documentation is written in Markdown using [Material for MkDocs](https://squidfunk.github.io/mkdocs-material/).
You can run the command `hatch run docs:serve` to start a server for viewing the documentation on localhost. When the command
starts, it prints the URL for viewing the docs locally, and will automatically update them when the `mkdocs.yml` configuration
or various markdown files are modified. The `hatch run docs:build` will build the documentation to static html content.

## Testing

The objective for the tests of this package are to act as regression tests to help identify unintended changes to
functionality in the package. As such, we strive to have high test coverage of the different behaviours/functionality
that the package contains. Code coverage metrics are not the goal, but rather are a guide to help identify places
where there may be gaps in testing coverage.

The tests for this package have three forms:

1. Unit tests - Small tests that are narrowly focused on ensuring that function-level behavior
   of the implementation behaves as it is expected to. These can always be run locally on your workstation
   without requiring an AWS account.
2. Integration tests - Tests that ensure that the implementation behaves as expected when run in a real environment.
   Ensuring that code properly interacts as expected with a real Amazon S3 bucket, for instance.

### Writing Tests

If you want assistance developing tests, then please don't hesitate to open a draft pull request and ask for help.
We'll do our best to help you out and point you in the right direction.

Our tests are implemented using the [PyTest](https://docs.pytest.org/en/stable/) testing framework,
and unit tests generally make use of Python's [unittest.mock](https://docs.python.org/3.8/library/unittest.mock.html)
package to avoid runtime dependencies and narrowly focus tests on a specific aspect of the implementation.

If you are not sure how to start writing tests, then we suggest looking at the existing tests
for the same or similar functions for inspiration (search for calls to the function within the `test/`
subdirectories). You will also find both the official [PyTest documentation](https://docs.pytest.org/en/stable/)
and [unitest.mock documentation](https://docs.python.org/3.8/library/unittest.mock.html) very informative (we do).

### Unit Tests

Unit tests are all located under the `test/unit` directory of this repository. If you are adding or modifying
functionality, then you will almost always want to be writing one or more unit tests to demonstrate that your
logic behaves as expected and that future changes do not accidentally break your change.

#### Running Unit Tests

You can run unit tests by running:

* `hatch run test` - To run the unit tests with your default Python runtime.
* `hatch run all:test` - To run the unit tests with all of the supported Python runtime versions that you have installed.

#### Running Docker-based Unit Tests

Some of the unit tests in this package require a docker environment to run. These tests are marked with `@pytest.mark.docker`.
In order to run these tests, please run the `run_sudo_tests.sh` script located in the `scripts` directory. For detailed instructions,
please refer to [scripts/README.md](./scripts/README.md).

If you make changes to the `download` or `asset_sync` modules, it's highly recommended to run and ensure these tests pass.

### Integration Tests

Integration tests are all located under the `test/integ` directory of this repository. You should consider
adding or modifying an integration test for any change that adds or modifies functionality that directly
interfaces with the local filesystem or an AWS service API.

#### Running Integration Tests

Our integration tests run using infrastructure that is in your AWS Account. A Farm, Queue and Fleet (that associated with 
the Queue) will be required to run the integration tests. The identifiers for these resources are communicated to the 
tests through environment variables that you must define before running the tests. Define the following environment 
variables:

```bash
# Replace with your AWS Account ID
export SERVICE_ACCOUNT_ID=000000000000
# Replace with the region code where your AWS test resources are located (e.g. us-west-2)
export AWS_DEFAULT_REGION=xx-yyyy-nn
# Replace with the ID of your AWS Deadline Cloud Farm
export FARM_ID=farm-00112233445566778899aabbccddeeff
# Replace with the ID of your AWS Deadline Cloud Queue that is configured with a
# Job Attachments bucket.
export QUEUE_ID=queue-00112233445566778899aabbccddeeff

export JOB_ATTACHMENTS_BUCKET=$(
   aws deadline get-queue --farm-id $FARM_ID --queue-id $QUEUE_ID \
    --query 'jobAttachmentSettings.s3BucketName' | tr -d '"'
)
export JA_TEST_ROOT_PREFIX=$(
   aws deadline get-queue --farm-id $FARM_ID --queue-id $QUEUE_ID \
    --query 'jobAttachmentSettings.rootPrefix' | tr -d '"'
)
```

Then you can run the integration tests with:

```bash
hatch run integ:test
```

Notes:
* If you are not one of the AWS Deadline Cloud developers then you may see test failures in tests marked with
  `pytest.mark.cross_account`. That's okay, just ignore them; they'll be tested with the required setup in our CI.
* If you are adding/changing code related to the Job Attachments' file-upload interactions with S3, then if you have a second
  AWS account then we request that you also ensure that the tests marked with the `pytest.mark.cross_account` marker also pass.
  If you don't have a second account, then don't worry about it. These tests will run in our CI. To run these tests:
  1. Create an S3 bucket in the same region as your testing resources but in your second AWS Account. If the bucket doesn't exist, you may see S3 PermanentRedirect error.
  2. Set the access policy of that S3 bucket to allow your first AWS Account to perform all operations on the bucket. Do
     NOT open the bucket up to the world for reading/writing!
  3. `export INTEG_TEST_JA_CROSS_ACCOUNT_BUCKET=<your-bucket-name-in-the-second-account>`
  4. Run the integration tests.

## Changelog Guidelines

When a new version of `deadline` is being released, we must prepare an update to our change log (`CHANGELOG.md`). This is a semi-automated process. GitHub actions prepares a pull request with an automatically generated draft of the changelog entry. Maintainers are responsible for reviewing the draft, making any necessary changes, and reviewing the changes in the pull request. Please consult in [CHANGELOG_GUIDELINES.md](./CHANGELOG_GUIDELINES.md) for the changelog guidelines. These guidelines ensure consistency in how we communicate changes to users and provide standards for:

* Structuring changelog sections and their ordering
* Writing user-focused descriptions for different types of changes
* Handling breaking changes with proper migration guidance
* Communicating deprecations effectively
* Managing fixes to unreleased changes
* Documenting changes to experimental features

## Things to Know

### Public Contracts

The publicly consumable interfaces of this library and CLI are all considered to be public contracts. Meaning that any
change to them that is not backwards compatible is considered to be a breaking change. We strive to avoid making breaking
changes when possible, but accept that there are sometimes very good reasons for why a breaking change is necessary.

The following are some heuristics to demonstrate how to think about breaking vs non-breaking changes in the public interface.

For the command-line interface:
* Things like adding a non-required argument to a subcommand, or adding a new subcommand are not breaking changes.
* Renaming a subcommand or argument is a breaking change.
* Adding a new required subcommand argument is a breaking change.
* Changing a default value/behaviour is a breaking change.

For the Python library interface:
* We follow the [PEP 8](https://peps.python.org/pep-0008/#descriptive-naming-styles) weak internal use indicator convention
  and name all functions and modules that are internal/private with a leading underscore character.
* All functions and modules whose name does not begin with an underscore are part of the public contract for this package.
* Things like adding a non-required keyword argument to a function, or adding a new public function are not breaking changes.
* Things like renaming a keyword argument, or adding/removing a positional argument in a public function is a breaking change.
* Changing a default argument value is a breaking change.
* Changing the location that a file or directory is created should be considered to be a breaking change. These locations have a tendancy to become
  de-facto parts of the public contract as users build automation that assumes these locations is unchanged.

Note that we enforce our public contract through GitHub actions. See the [API Change Detection section](scripts/README.md#api-change-detection) in the scripts README for more information about generating and validating API changes.

#### Private Modules

New code should reside in private modules (example: `_my_module.py`), which removes the need to mark imports, classes, and functions as private with an underscore.

```python
# _my_module.py
import os

class PublicClass:
    def publicmethod(self):
        pass
    # We still need to mark this as private, since the class will be public
    def _privatemethod(self):
        pass

class PrivateClass:
    def privatemethod(self):
        pass
```

Public contracts in private modules are defined by imports in the corresponding `__init__.py` in the same directory as the private module.

```python
# __init__.py

from _my_module import PublicClass
```

#### Public Modules

A public module (for example `my_module.py`) in this package will be defined with the following style:

```python
# my_module.py

# The os module is not part of this file's external interface
import os as _os

# PublicClass is part of this file's external interface.
class PublicClass:
    def publicmethod(self):
        pass

    def _privatemethod(self):
        pass

# _PrivateClass is not part of this file's external interface.
class _PrivateClass:
    def publicmethod(self):
        pass

    def _privatemethod(self):
        pass
```

#### On `import os as _os`

Every module/symbol that is imported into a Python module becomes a part of that module's interface.
Thus, if we have a module called `foo.py` such as:

```python
# foo.py

import os
```

Then, the `os` module becomes part of the public interface for `foo.py` and a consumer of that module
is free to do:

```python
from foo import os
```

We don't want all (generally, we don't want any) of our imports to become part of the public API for
the module, so we import modules/symbols into a public module with the following style:

```python
import os as _os
from typing import Dict as _Dict
```

### Library Dependencies

Library dependencies are Python packages required to build and run the Deadline Cloud Python project. Dependencies are specified in the `dependencies` section of `pyproject.toml`.

The Deadline Cloud job attachments library is designed to be integrated into third-party applications that have bespoke and customized deployment environments. Adding dependencies will increase the chance of library version conflicts and incompatabilities. Please evaluate the addition of each new dependency.

We try to minimize the number of dependencies required to build and run Deadline Cloud. When contributing changes, please consider the following.

#### Why is a new dependency needed?

* Is the dependency library functionality required small enough to have a minimal version added to the Deadline Cloud code base?

#### Quality of the dependency

* Is the dependency active, reputable or maintained by a reputable source? Considerations can include:
    - PyPI download stats
    - GitHub stars
    - GitHub dependency graph showing downstream consumers
* Is it well-maintained?
* Is the library released regularly or recently?

#### Version Pinning

* How should we pin the version of this new dependency?
    - Please consider changes over time such as API or CLI command evolution and breakage.
* Does the library follow a versioning scheme such as semver?

#### Licensing

*   Please ensure the license of the dependency is compatible with the distribution license of this library.
