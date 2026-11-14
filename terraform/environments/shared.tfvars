# Team access, in git: add a person here in a pull request, merge, and the
# pipeline creates their IAM user in the right group. An admin then sets their
# console password once; they add MFA at first sign-in (roles require it).
#
# Groups: admins, developers, readonly (see terraform/shared/team.tf).

region = "us-east-1"

team_members = {
  # "priya" = "developers"
  # "sam"   = "readonly"
}
