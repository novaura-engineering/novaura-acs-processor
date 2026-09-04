#!/bin/bash

# Resolve the image tag: an explicit override wins, otherwise the current commit.
IMAGE_TAG="${IMAGE_TAG:-$(git rev-parse HEAD)}"

# Verify the tag exists in ECR before planning.
#
# Terraform will happily plan and apply a task definition pointing at an image that was
# never built. It succeeds, and the failure only surfaces later as tasks stuck in PENDING
# with CannotPullContainerError. That is easy to hit because CI tags images with
# github.sha -- for a pull_request build that is the merge-test commit, not the branch
# HEAD this script would otherwise use -- so a local run after a merge can name a tag
# nothing ever pushed.
ECR_REPOSITORY="${ECR_REPOSITORY:-novaura-acs-processor}"
AWS_REGION="${AWS_REGION:-us-east-1}"

if ! AWS_PROFILE=terraform-deployer aws ecr describe-images \
        --repository-name "$ECR_REPOSITORY" \
        --image-ids "imageTag=$IMAGE_TAG" \
        --region "$AWS_REGION" >/dev/null 2>&1; then
    echo "ERROR: no image tagged '$IMAGE_TAG' in ECR repository '$ECR_REPOSITORY'." >&2
    echo >&2
    echo "Applying this would point the task definitions at an image that does not exist," >&2
    echo "and every new task would fail with CannotPullContainerError." >&2
    echo >&2
    echo "Most recently pushed images (newest first):" >&2
    # describe-images paginates and the CLI applies --query per page, so sorting and
    # slicing server-side silently returns per-page results. Sort locally instead.
    AWS_PROFILE=terraform-deployer aws ecr describe-images \
        --repository-name "$ECR_REPOSITORY" \
        --region "$AWS_REGION" \
        --query 'imageDetails[?imageTags!=`null`].[imagePushedAt,join(`,`,imageTags)]' \
        --output text 2>/dev/null | sort -r | head -5 | sed 's/^/  /' >&2
    echo >&2
    echo "Pick one of those, or push a build for this commit first:" >&2
    echo "  IMAGE_TAG=<tag> $0 $*" >&2
    exit 1
fi

# Ensure we're in the terraform directory
cd "$(dirname "$0")/terraform" || exit 1

# Apply with the specific image tag
AWS_PROFILE=terraform-deployer terraform apply -var="image_tag=$IMAGE_TAG" "$@" 