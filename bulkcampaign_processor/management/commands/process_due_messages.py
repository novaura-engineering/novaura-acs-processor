from django.core.management.base import BaseCommand
from bulkcampaign_processor.services.bulk_campaign_processor import BulkCampaignProcessor
from bulkcampaign_processor.tasks.bulk_campaign_tasks import process_due_messages, process_retry_messages
from bulkcampaign_processor.utils.pass_loop import PassLoop
import logging

logger = logging.getLogger('bulkcampaign_processor')

DEFAULT_LOOP_INTERVAL_SECONDS = 30


class Command(BaseCommand):
    help = 'Process due bulk campaign messages: one pass, or continuously with --loop'

    def add_arguments(self, parser):
        parser.add_argument(
            '--retry-only',
            action='store_true',
            help='Only process retry messages',
        )
        parser.add_argument(
            '--loop',
            action='store_true',
            help='Keep running, one pass every --interval seconds, until SIGTERM. '
                 'This is how the ECS worker service runs it.',
        )
        parser.add_argument(
            '--interval',
            type=float,
            default=DEFAULT_LOOP_INTERVAL_SECONDS,
            help=f'Seconds between the end of one pass and the start of the next '
                 f'(default {DEFAULT_LOOP_INTERVAL_SECONDS}).',
        )

    def handle(self, *args, **options):
        if options['loop']:
            if options['retry_only']:
                self.stderr.write(self.style.ERROR('--loop does not support --retry-only'))
                return
            processor = BulkCampaignProcessor()
            loop = PassLoop(
                'process_due_messages',
                lambda should_stop: process_due_messages(should_stop=should_stop, processor=processor),
                options['interval'],
            )
            loop.install_signal_handlers()
            loop.run()
            return

        try:
            if options['retry_only']:
                processed_count = process_retry_messages()
                self.stdout.write(self.style.SUCCESS(f'Successfully processed {processed_count} retry messages'))
            else:
                processed_count = process_due_messages()
                self.stdout.write(self.style.SUCCESS(f'Successfully processed {processed_count} due messages'))
        except Exception as e:
            logger.error(f"Error processing messages: {str(e)}")
            self.stdout.write(self.style.ERROR(f'Error processing messages: {str(e)}'))
