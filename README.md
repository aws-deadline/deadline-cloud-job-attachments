# AWS Deadline Cloud Job Attachments

### [User guide](https://aws-deadline.github.io/) | [Service documentation](https://docs.aws.amazon.com/deadline-cloud/) | [Deadline Cloud on GitHub](https://github.com/aws-deadline/) 

[![pypi](https://img.shields.io/pypi/v/deadline-job-attachments.svg?style=flat)](https://pypi.python.org/pypi/deadline-job-attachments)
[![python](https://img.shields.io/pypi/pyversions/deadline-job-attachments.svg?style=flat)](https://pypi.python.org/pypi/deadline-job-attachments)
[![license](https://img.shields.io/pypi/l/deadline-job-attachments.svg?style=flat)](https://github.com/aws-deadline/deadline-cloud-job-attachments/blob/mainline/LICENSE)

Job attachments enable you to transfer files between your workstations and AWS Deadline Cloud using Amazon S3 buckets as
[content-addressed storage][cas] in your AWS account. The use of a content-addressed storage means that a file will never need
to be uploaded again once it has been uploaded once.

See [job attachments][job-attachments] for a more in-depth look at how files are uploaded, stored, and retrieved.

[cas]: https://en.wikipedia.org/wiki/Content-addressable_storage
[job-attachments]: https://docs.aws.amazon.com/deadline-cloud/latest/developerguide/build-job-attachments.html

## Compatibility

This library requires:

1. Python 3.8 through 3.14; and
2. Linux, Windows, or macOS operating system.

## Versioning

This package's version follows [Semantic Versioning 2.0](https://semver.org/), but is still considered to be in its
initial development, thus backwards incompatible versions are denoted by minor version bumps. To help illustrate how
versions will increment during this initial development stage, they are described below:

1. The MAJOR version is currently 0, indicating initial development.
2. The MINOR version is currently incremented when backwards incompatible changes are introduced to the public API.
3. The PATCH version is currently incremented when bug fixes or backwards compatible changes are introduced to the public API.

## Getting Started

AWS Deadline Cloud job attachments can be installed by the standard python packaging mechanisms:
```sh
$ pip install deadline-job-attachments
```

## Contributing

We welcome all contributions. Please see [CONTRIBUTING.md](https://github.com/aws-deadline/deadline-cloud-job-attachments/blob/mainline/CONTRIBUTING.md)
for guidance on how to contribute. Please report issues such as bugs, inaccurate or confusing information, and so on,
by making feature requests in the [issue tracker](https://github.com/aws-deadline/deadline-cloud-job-attachments/issues). We encourage
code contributions in the form of [pull requests](https://github.com/aws-deadline/deadline-cloud-job-attachments/pulls).

## Code of Conduct

This project has adopted the [Amazon Open Source Code of Conduct](https://aws.github.io/code-of-conduct).
For more information see the [Code of Conduct FAQ](https://aws.github.io/code-of-conduct-faq) or contact
opensource-codeofconduct@amazon.com with any additional questions or comments.

## Security Issue Notifications

We take all security reports seriously. When we receive such reports, we will
investigate and subsequently address any potential vulnerabilities as quickly
as possible. If you discover a potential security issue in this project, please
notify AWS/Amazon Security via our [vulnerability reporting page](http://aws.amazon.com/security/vulnerability-reporting/)
or directly via email to [AWS Security](mailto:aws-security@amazon.com). Please do not
create a public GitHub issue in this project.

## License

This project is licensed under the Apache-2.0 License.
