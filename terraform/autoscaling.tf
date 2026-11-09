# Auto-scaling for Journey Worker
resource "aws_appautoscaling_target" "journey_worker_target" {
  max_capacity       = 0
  min_capacity       = 0
  resource_id        = "service/${aws_ecs_cluster.processor_cluster.name}/${aws_ecs_service.journey_worker_service.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}

resource "aws_appautoscaling_policy" "journey_worker_policy" {
  name               = "novaura-acs-journey-worker-scaling"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.journey_worker_target.resource_id
  scalable_dimension = aws_appautoscaling_target.journey_worker_target.scalable_dimension
  service_namespace  = aws_appautoscaling_target.journey_worker_target.service_namespace
  
  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
    target_value       = 70.0
    scale_in_cooldown  = 300  # 5 minutes
    scale_out_cooldown = 60   # 1 minute
  }
}

# Bulk Campaign Worker: deliberately NOT autoscaled.
#
# It scaled on CPU, but the worker's CPU was Django booting: the service ran a one-shot
# command that ECS relaunched ~300 times an hour, and those boot spikes held it at the
# maximum of ten tasks. Ten tasks then raced each other over the same due messages and
# sent duplicates. Its throughput is set by the campaigns' send caps, not by CPU, so the
# count is fixed in ecs.tf (var.bulk_worker_count).

# Auto-scaling for SMS Marketing Worker
resource "aws_appautoscaling_target" "sms_marketing_worker_target" {
  max_capacity       = 10
  min_capacity       = 1
  resource_id        = "service/${aws_ecs_cluster.processor_cluster.name}/${aws_ecs_service.sms_marketing_worker_service.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}

resource "aws_appautoscaling_policy" "sms_marketing_worker_policy" {
  name               = "novaura-acs-sms-marketing-worker-scaling"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.sms_marketing_worker_target.resource_id
  scalable_dimension = aws_appautoscaling_target.sms_marketing_worker_target.scalable_dimension
  service_namespace  = aws_appautoscaling_target.sms_marketing_worker_target.service_namespace
  
  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
    target_value       = 70.0
    scale_in_cooldown  = 300  # 5 minutes
    scale_out_cooldown = 60   # 1 minute
  }
} 