resource "aws_ecs_cluster" "processor_cluster" {
  name = "novaura-acs-processor-cluster"
  
  setting {
    name  = "containerInsights"
    value = "enabled"
  }
  
  tags = {
    Name        = "Novaura ACS Processor Cluster"
    Environment = var.environment
  }
}

resource "aws_cloudwatch_log_group" "processor_logs" {
  name              = "/ecs/novaura-acs-processor"
  retention_in_days = 30
  
  tags = {
    Name        = "Novaura ACS Processor Logs"
    Environment = var.environment
  }
}

# Bulk Campaign Scheduler Task
resource "aws_ecs_task_definition" "bulk_campaign_scheduler_task" {
  family                   = "novaura-acs-bulk-campaign-scheduler"
  cpu                      = var.scheduler_cpu
  memory                   = var.scheduler_memory
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  execution_role_arn       = aws_iam_role.ecs_execution_role.arn
  task_role_arn            = aws_iam_role.ecs_task_role.arn
  
  container_definitions = jsonencode([
    {
      name      = "bulk-campaign-scheduler"
      image     = "${data.aws_ecr_repository.processor_repository.repository_url}:${var.image_tag}"
      essential = true
      # --loop: one long-lived process, a pass every 5 minutes. As a bare one-shot command
      # in an always-on service, ECS relaunched it on every exit and it re-scanned every
      # participant back to back. SIGTERM finishes the current campaign, then exits.
      # Documentation only: docker-entrypoint.sh picks the command from SERVICE_TYPE
      # and ignores this. Keep the two in step.
      command     = ["python", "manage.py", "process_bulk_campaigns", "--loop"]
      stopTimeout = 120
      
      environment = [
        { name = "SERVICE_TYPE", value = "bulk_campaign_scheduler" },
        { name = "JOURNEY_EVENTS_QUEUE_URL", value = aws_sqs_queue.journey_events.url },
        { name = "DB_NAME", value = var.db_name },
        { name = "DB_USER", value = var.db_user },
        { name = "DB_HOST", value = var.db_host },
        { name = "DB_PORT", value = "3306" },
        { name = "DJANGO_SETTINGS_MODULE", value = "acs_personalization.settings.prod" }
      ]
      
      secrets = [
        { 
          name = "DB_PASSWORD", 
          valueFrom = "${var.db_password_arn}:DB_PASSWORD::" 
        },
        { 
          name = "DJANGO_SECRET_KEY", 
          valueFrom = "${var.django_secret_key_arn}:DJANGO_SECRET_KEY::" 
        }
      ]
      
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.processor_logs.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "bulk-campaign-scheduler"
        }
      }
    }
  ])
  
  tags = {
    Name        = "Novaura ACS Bulk Campaign Scheduler Task"
    Environment = var.environment
  }
}

# Journey Scheduler Task
resource "aws_ecs_task_definition" "journey_scheduler_task" {
  family                   = "novaura-acs-journey-scheduler"
  cpu                      = var.scheduler_cpu
  memory                   = var.scheduler_memory
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  execution_role_arn       = aws_iam_role.ecs_execution_role.arn
  task_role_arn            = aws_iam_role.ecs_task_role.arn
  
  container_definitions = jsonencode([
    {
      name      = "journey-scheduler"
      image     = "${data.aws_ecr_repository.processor_repository.repository_url}:${var.image_tag}"
      essential = true
      command   = ["python", "manage.py", "run_scheduler"]
      
      environment = [
        { name = "SERVICE_TYPE", value = "journey_scheduler" },
        { name = "JOURNEY_EVENTS_QUEUE_URL", value = aws_sqs_queue.journey_events.url },
        { name = "DB_NAME", value = var.db_name },
        { name = "DB_USER", value = var.db_user },
        { name = "DB_HOST", value = var.db_host },
        { name = "DB_PORT", value = "3306" },
        { name = "DJANGO_SETTINGS_MODULE", value = "acs_personalization.settings.prod" }
      ]
      
      secrets = [
        { 
          name = "DB_PASSWORD", 
          valueFrom = "${var.db_password_arn}:DB_PASSWORD::" 
        },
        { 
          name = "DJANGO_SECRET_KEY", 
          valueFrom = "${var.django_secret_key_arn}:DJANGO_SECRET_KEY::" 
        }
      ]
      
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.processor_logs.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "journey-scheduler"
        }
      }
    }
  ])
  
  tags = {
    Name        = "Novaura ACS Journey Scheduler Task"
    Environment = var.environment
  }
}

# Journey Worker Task
resource "aws_ecs_task_definition" "journey_worker_task" {
  family                   = "novaura-acs-journey-worker"
  cpu                      = var.worker_cpu
  memory                   = var.worker_memory
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  execution_role_arn       = aws_iam_role.ecs_execution_role.arn
  task_role_arn            = aws_iam_role.ecs_task_role.arn
  
  container_definitions = jsonencode([
    {
      name      = "journey-worker"
      image     = "${data.aws_ecr_repository.processor_repository.repository_url}:${var.image_tag}"
      essential = true
      command   = ["python", "manage.py", "run_worker"]
      
      environment = [
        { name = "SERVICE_TYPE", value = "journey_worker" },
        { name = "JOURNEY_EVENTS_QUEUE_URL", value = aws_sqs_queue.journey_events.url },
        { name = "DB_NAME", value = var.db_name },
        { name = "DB_USER", value = var.db_user },
        { name = "DB_HOST", value = var.db_host },
        { name = "DB_PORT", value = "3306" },
        { name = "DJANGO_SETTINGS_MODULE", value = "acs_personalization.settings.prod" }
      ]
      
      secrets = [
        { 
          name = "DB_PASSWORD", 
          valueFrom = "${var.db_password_arn}:DB_PASSWORD::" 
        },
        { 
          name = "DJANGO_SECRET_KEY", 
          valueFrom = "${var.django_secret_key_arn}:DJANGO_SECRET_KEY::" 
        },
        {
          name = "TWILIO_ACCOUNT_SID",
          valueFrom = "${var.twilio_credentials_arn}:TWILIO_ACCOUNT_SID::"
        },
        {
          name = "TWILIO_AUTH_TOKEN",
          valueFrom = "${var.twilio_credentials_arn}:TWILIO_AUTH_TOKEN::"
        },
        {
          name = "BLAND_AI_API_KEY",
          valueFrom = "${var.bland_ai_api_key_arn}:BLAND_AI_API_KEY::"
        }
      ]
      
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.processor_logs.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "journey-worker"
        }
      }
    }
  ])
  
  tags = {
    Name        = "Novaura ACS Journey Worker Task"
    Environment = var.environment
  }
}

# Bulk Campaign Worker Task
resource "aws_ecs_task_definition" "bulk_campaign_worker_task" {
  family                   = "novaura-acs-bulk-campaign-worker"
  cpu                      = var.worker_cpu
  memory                   = var.worker_memory
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  execution_role_arn       = aws_iam_role.ecs_execution_role.arn
  task_role_arn            = aws_iam_role.ecs_task_role.arn
  
  container_definitions = jsonencode([
    {
      name      = "bulk-campaign-worker"
      image     = "${data.aws_ecr_repository.processor_repository.repository_url}:${var.image_tag}"
      essential = true
      # --loop: one long-lived process, a pass every 30 seconds. As a bare one-shot command
      # in an always-on service, ECS relaunched it ~300 times an hour and every start
      # re-read the whole due set. SIGTERM finishes the current group, then exits.
      # Documentation only: docker-entrypoint.sh picks the command from SERVICE_TYPE
      # and ignores this. Keep the two in step.
      command     = ["python", "manage.py", "process_due_messages", "--loop"]
      stopTimeout = 60
      
      environment = [
        { name = "SERVICE_TYPE", value = "bulk_campaign_worker" },
        { name = "JOURNEY_EVENTS_QUEUE_URL", value = aws_sqs_queue.journey_events.url },
        { name = "DB_NAME", value = var.db_name },
        { name = "DB_USER", value = var.db_user },
        { name = "DB_HOST", value = var.db_host },
        { name = "DB_PORT", value = "3306" },
        { name = "DJANGO_SETTINGS_MODULE", value = "acs_personalization.settings.prod" }
      ]
      
      secrets = [
        { 
          name = "DB_PASSWORD", 
          valueFrom = "${var.db_password_arn}:DB_PASSWORD::" 
        },
        { 
          name = "DJANGO_SECRET_KEY", 
          valueFrom = "${var.django_secret_key_arn}:DJANGO_SECRET_KEY::" 
        },
        {
          name = "TWILIO_ACCOUNT_SID",
          valueFrom = "${var.twilio_credentials_arn}:TWILIO_ACCOUNT_SID::"
        },
        {
          name = "TWILIO_AUTH_TOKEN",
          valueFrom = "${var.twilio_credentials_arn}:TWILIO_AUTH_TOKEN::"
        },
        {
          name = "BLAND_AI_API_KEY",
          valueFrom = "${var.bland_ai_api_key_arn}:BLAND_AI_API_KEY::"
        }
      ]
      
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.processor_logs.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "bulk-campaign-worker"
        }
      }
    }
  ])
  
  tags = {
    Name        = "Novaura ACS Bulk Campaign Worker Task"
    Environment = var.environment
  }
}

# Communication Processor Worker Task
resource "aws_ecs_task_definition" "communication_processor_worker_task" {
  family                   = "novaura-acs-communication-processor-worker"
  cpu                      = var.worker_cpu
  memory                   = var.worker_memory
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  execution_role_arn       = aws_iam_role.ecs_execution_role.arn
  task_role_arn            = aws_iam_role.ecs_task_role.arn
  
  container_definitions = jsonencode([
    {
      name      = "communication-processor-worker"
      image     = "${data.aws_ecr_repository.processor_repository.repository_url}:${var.image_tag}"
      essential = true
      command   = ["python", "communication_processor/worker.py"]
      
      environment = [
        { name = "SERVICE_TYPE", value = "communication_processor_worker" },
        { name = "WORKER_TYPE", value = "all" },
        { name = "SMS_QUEUE_URL", value = aws_sqs_queue.sms_events.url },
        { name = "EMAIL_QUEUE_URL", value = aws_sqs_queue.email_events.url },
        { name = "DB_NAME", value = var.db_name },
        { name = "DB_USER", value = var.db_user },
        { name = "DB_HOST", value = var.db_host },
        { name = "DB_PORT", value = "3306" },
        { name = "DJANGO_SETTINGS_MODULE", value = "acs_personalization.settings.prod" }
      ]
      
      secrets = [
        { 
          name = "DB_PASSWORD", 
          valueFrom = "${var.db_password_arn}:DB_PASSWORD::" 
        },
        { 
          name = "DJANGO_SECRET_KEY", 
          valueFrom = "${var.django_secret_key_arn}:DJANGO_SECRET_KEY::" 
        },
        {
          name = "TWILIO_ACCOUNT_SID",
          valueFrom = "${var.twilio_credentials_arn}:TWILIO_ACCOUNT_SID::"
        },
        {
          name = "TWILIO_AUTH_TOKEN",
          valueFrom = "${var.twilio_credentials_arn}:TWILIO_AUTH_TOKEN::"
        },
        {
          name = "BLAND_AI_API_KEY",
          valueFrom = "${var.bland_ai_api_key_arn}:BLAND_AI_API_KEY::"
        }
      ]
      
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.processor_logs.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "communication-processor-worker"
        }
      }
    }
  ])
  
  tags = {
    Name        = "Novaura ACS Communication Processor Worker Task"
    Environment = var.environment
  }
}

# SMS Marketing Worker Task
resource "aws_ecs_task_definition" "sms_marketing_worker_task" {
  family                   = "novaura-acs-sms-marketing-worker"
  cpu                      = var.worker_cpu
  memory                   = var.worker_memory
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  execution_role_arn       = aws_iam_role.ecs_execution_role.arn
  task_role_arn            = aws_iam_role.ecs_task_role.arn
  
  container_definitions = jsonencode([
    {
      name      = "sms-marketing-worker"
      image     = "${data.aws_ecr_repository.processor_repository.repository_url}:${var.image_tag}"
      essential = true
      command   = ["python", "manage.py", "run_sms_marketing_worker"]
      
      environment = [
        { name = "SERVICE_TYPE", value = "sms_marketing_worker" },
        { name = "SMS_MARKETING_QUEUE_URL", value = aws_sqs_queue.sms_marketing_events.url },
        { name = "SMS_MARKETING_DLQ_URL", value = aws_sqs_queue.sms_marketing_events_dlq.url },
        { name = "DB_NAME", value = var.db_name },
        { name = "DB_USER", value = var.db_user },
        { name = "DB_HOST", value = var.db_host },
        { name = "DB_PORT", value = "3306" },
        { name = "DJANGO_SETTINGS_MODULE", value = "acs_personalization.settings.prod" }
      ]
      
      secrets = [
        { 
          name = "DB_PASSWORD", 
          valueFrom = "${var.db_password_arn}:DB_PASSWORD::" 
        },
        { 
          name = "DJANGO_SECRET_KEY", 
          valueFrom = "${var.django_secret_key_arn}:DJANGO_SECRET_KEY::" 
        },
        {
          name = "TWILIO_ACCOUNT_SID",
          valueFrom = "${var.twilio_credentials_arn}:TWILIO_ACCOUNT_SID::"
        },
        {
          name = "TWILIO_AUTH_TOKEN",
          valueFrom = "${var.twilio_credentials_arn}:TWILIO_AUTH_TOKEN::"
        },
        {
          name = "BLAND_AI_API_KEY",
          valueFrom = "${var.bland_ai_api_key_arn}:BLAND_AI_API_KEY::"
        }
      ]
      
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.processor_logs.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "sms-marketing-worker"
        }
      }
    }
  ])
  
  tags = {
    Name        = "Novaura ACS SMS Marketing Worker Task"
    Environment = var.environment
  }
}

# Services
resource "aws_ecs_service" "bulk_campaign_scheduler_service" {
  name            = "novaura-acs-bulk-campaign-scheduler"
  cluster         = aws_ecs_cluster.processor_cluster.id
  task_definition = aws_ecs_task_definition.bulk_campaign_scheduler_task.arn
  desired_count   = var.bulk_scheduler_count
  launch_type     = "FARGATE"

  # Stop the old scheduler before starting the new one on deploy: two schedulers
  # walking the same participants at once only race each other.
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100
  
  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups = [aws_security_group.ecs_service.id]
  }
  
  tags = {
    Name        = "Novaura ACS Bulk Campaign Scheduler Service"
    Environment = var.environment
  }
}

resource "aws_ecs_service" "journey_scheduler_service" {
  name            = "novaura-acs-journey-scheduler"
  cluster         = aws_ecs_cluster.processor_cluster.id
  task_definition = aws_ecs_task_definition.journey_scheduler_task.arn
  desired_count   = var.journey_worker_count
  launch_type     = "FARGATE"
  
  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups = [aws_security_group.ecs_service.id]
  }
  
  tags = {
    Name        = "Novaura ACS Journey Scheduler Service"
    Environment = var.environment
  }
}

resource "aws_ecs_service" "journey_worker_service" {
  name            = "novaura-acs-journey-worker"
  cluster         = aws_ecs_cluster.processor_cluster.id
  task_definition = aws_ecs_task_definition.journey_worker_task.arn
  desired_count   = var.journey_worker_count
  launch_type     = "FARGATE"
  
  network_configuration {
    subnets          = concat(var.private_subnet_ids, var.public_subnet_ids)
    security_groups  = [aws_security_group.ecs_service.id]
    assign_public_ip = false
  }
  
  tags = {
    Name        = "Novaura ACS Journey Worker Service"
    Environment = var.environment
  }
}

resource "aws_ecs_service" "bulk_campaign_worker_service" {
  name            = "novaura-acs-bulk-campaign-worker"
  cluster         = aws_ecs_cluster.processor_cluster.id
  task_definition = aws_ecs_task_definition.bulk_campaign_worker_task.arn
  # Fixed, not autoscaled: workers claim message groups (SELECT ... FOR UPDATE SKIP
  # LOCKED), so more than one is safe, but send caps bound throughput far below what one
  # worker can do. See var.bulk_worker_count.
  desired_count = var.bulk_worker_count
  launch_type   = "FARGATE"

  # Stop old workers before starting new ones. Old task definitions may not take the
  # group lock -- the first deploy of this change replaces ten unlocked workers -- and an
  # old and a new worker side by side could both send the same message. A short gap
  # with no worker only delays sends by a pass.
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100
  
  network_configuration {
    subnets          = var.public_subnet_ids # Using only the private-nat subnets for outbound internet access
    security_groups  = [aws_security_group.ecs_service.id]
    assign_public_ip = false
  }
  
  tags = {
    Name        = "Novaura ACS Bulk Campaign Worker Service"
    Environment = var.environment
  }
}

# Communication Processor Worker Service
resource "aws_ecs_service" "communication_processor_worker_service" {
  name            = "novaura-acs-communication-processor-worker-service"
  cluster         = aws_ecs_cluster.processor_cluster.id
  task_definition = aws_ecs_task_definition.communication_processor_worker_task.arn
  desired_count   = var.communication_worker_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets         = var.public_subnet_ids
    security_groups = [aws_security_group.ecs_service.id]
    assign_public_ip = true
  }

  depends_on = [aws_ecs_cluster.processor_cluster]
  
  tags = {
    Name        = "Novaura ACS Communication Processor Worker Service"
    Environment = var.environment
  }
}

# SMS Marketing Worker Service
resource "aws_ecs_service" "sms_marketing_worker_service" {
  name            = "novaura-acs-sms-marketing-worker-service"
  cluster         = aws_ecs_cluster.processor_cluster.id
  task_definition = aws_ecs_task_definition.sms_marketing_worker_task.arn
  desired_count   = var.sms_marketing_worker_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets         = var.public_subnet_ids
    security_groups = [aws_security_group.ecs_service.id]
    assign_public_ip = true
  }

  depends_on = [aws_ecs_cluster.processor_cluster]
  
  tags = {
    Name        = "Novaura ACS SMS Marketing Worker Service"
    Environment = var.environment
  }
} 