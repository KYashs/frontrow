"""FrontRow core: inventory rules shared by the API (EKS) and the Lambda functions.

Kept Python 3.9 compatible (Amazon Linux 2023 system Python) and dependency
free apart from boto3, so the same code ships in the app bundle and in
every Lambda zip.
"""
