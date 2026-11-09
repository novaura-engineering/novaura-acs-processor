terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 4.0"
    }
  }
  backend "s3" {
    bucket         = "novaura-terraform-state"
    key            = "ecs/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "terraform-locks"
    encrypt        = true
  }
}

provider "aws" {
  region = var.aws_region
}

# Variables
variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
}

variable "environment" {
  description = "Environment name"
  type        = string
  default     = "production"
}

variable "scheduler_cpu" {
  description = "CPU units for scheduler task"
  type        = number
  default     = 256
}

variable "scheduler_memory" {
  description = "Memory for scheduler task"
  type        = number
  default     = 512
}

variable "worker_cpu" {
  description = "CPU units for worker task"
  type        = number
  default     = 256
}

variable "worker_memory" {
  description = "Memory for worker task"
  type        = number
  default     = 512
}

variable "journey_worker_count" {
  description = "Number of worker tasks"
  type        = number
  default     = 2
}

variable "bulk_worker_count" {
  description = "Number of bulk-campaign worker tasks (process_due_messages --loop). Workers claim message groups with SKIP LOCKED, so >1 is safe, but send caps -- not CPU -- bound throughput; one is enough at current volume."
  type        = number
  default     = 1
}

variable "bulk_scheduler_count" {
  description = "Number of bulk-campaign scheduler tasks (process_bulk_campaigns --loop). Keep at 1."
  type        = number
  default     = 1

  validation {
    condition     = var.bulk_scheduler_count <= 1
    error_message = "Run at most one bulk-campaign scheduler; two would schedule the same participants concurrently."
  }
}

variable "communication_worker_count" {
  description = "Number of worker tasks"
  type        = number
  default     = 2
}

variable "sms_marketing_worker_count" {
  description = "Number of SMS marketing worker tasks"
  type        = number
  default     = 1
}

variable "ecr_repository_url" {
  description = "ECR repository URL"
  type        = string
}

variable "image_tag" {
  description = "Docker image tag to deploy. When deploying locally, you should specify the exact commit SHA or tag you want to deploy. Using 'latest' is not recommended for production deployments."
  type        = string
  default     = "latest"

  validation {
    condition     = can(regex("^[a-zA-Z0-9][a-zA-Z0-9_.-]*$", var.image_tag))
    error_message = "The image_tag value must be a valid Docker tag."
  }
}

variable "db_name" {
  description = "Database name"
  type        = string
}

variable "db_user" {
  description = "Database user"
  type        = string
}

variable "db_host" {
  description = "Database host"
  type        = string
}

variable "db_password_arn" {
  description = "ARN of the database password in Secrets Manager"
  type        = string
}

variable "django_secret_key_arn" {
  description = "ARN of the Django secret key in Secrets Manager"
  type        = string
}

variable "rds_security_group_id" {
  description = "Security group ID of the RDS instance"
  type        = string
}

variable "twilio_credentials_arn" {
  description = "ARN of the Secrets Manager secret containing Twilio credentials"
  type        = string
}

variable "bland_ai_api_key_arn" {
  description = "ARN of the Secrets Manager secret containing Bland AI API key"
  type        = string
}

# VPC and subnets (from shared networking stack / main Terraform repo or SSM)
variable "vpc_id" {
  description = "VPC ID where ECS and shared endpoints run (from main Terraform repo or SSM)"
  type        = string
}

variable "private_subnet_ids" {
  description = "Private subnet IDs for ECS tasks (from main Terraform repo or SSM)"
  type        = list(string)
}

variable "public_subnet_ids" {
  description = "Public (private-nat) subnet IDs for ECS workers that need outbound internet (from main Terraform repo or SSM)"
  type        = list(string)
}

variable "vpc_endpoints_security_group_id" {
  description = "Security group ID for VPC endpoints (from main Terraform repo or SSM). Used for reference/output only; endpoints are managed in the shared repo."
  type        = string
} 