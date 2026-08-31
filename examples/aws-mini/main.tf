# This file exists as the from-repo acceptance fixture for prizmanyx-foundry
# (Task 5, src/foundry/from_repo.py). `foundry create <name> --from-repo
# https://github.com/<owner>/prizmanyx-foundry --path examples/aws-mini`
# shallow-clones this repo and applies exactly this directory's contents
# against that environment's own floci endpoint.
#
# Deliberately carries NO `terraform {}` / `provider "aws" {}` block of its
# own: foundry injects both via an `override.tf` it writes into the
# terraform workdir at apply time (the same provider/endpoints block
# materialize.emit_terraform renders for a spec-based environment, reused
# verbatim - see from_repo.render_override_tf). Terraform's own
# override-file semantics add a provider block that has no match in the
# rest of the directory exactly as though it had been declared in an
# ordinary configuration file, so this fixture needs nothing more here.
#
# A tiny, self-contained, deliberately mixed estate - one of each of four
# resource types, one depending on another - so the acceptance run stays
# fast and its result (an `aws_s3_bucket` visible via boto3 against the
# env's own floci endpoint) stays unambiguous.

resource "aws_vpc" "main" {
  cidr_block = "10.99.0.0/16"
}

resource "aws_subnet" "main" {
  vpc_id            = aws_vpc.main.id
  cidr_block        = "10.99.0.0/24"
  availability_zone = "us-east-1a"
}

resource "aws_s3_bucket" "example" {
  bucket = "prizmanyx-foundry-aws-mini-example"
}

resource "aws_sqs_queue" "example" {
  name = "prizmanyx-foundry-aws-mini-example"
}
