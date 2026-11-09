from django.core.management.base import BaseCommand
from bulkcampaign_processor.tasks.bulk_campaign_tasks import process_bulk_campaigns
from bulkcampaign_processor.utils.pass_loop import PassLoop
import logging
import os

logger = logging.getLogger('bulkcampaign_processor')

DEFAULT_LOOP_INTERVAL_SECONDS = 300


class Command(BaseCommand):
    help = 'Schedule messages for active bulk campaigns: one pass, or continuously with --loop'

    def add_arguments(self, parser):
        parser.add_argument(
            '--loop',
            action='store_true',
            help='Keep running, one pass every --interval seconds, until SIGTERM. '
                 'This is how the ECS scheduler service runs it.',
        )
        parser.add_argument(
            '--interval',
            type=float,
            default=DEFAULT_LOOP_INTERVAL_SECONDS,
            help=f'Seconds between the end of one pass and the start of the next '
                 f'(default {DEFAULT_LOOP_INTERVAL_SECONDS}).',
        )

    def handle(self, *args, **options):
        # Disable profiling
        os.environ['PYDEVD_DISABLE_FILE_VALIDATION'] = '1'

        if options['loop']:
            loop = PassLoop('process_bulk_campaigns', process_bulk_campaigns, options['interval'])
            loop.install_signal_handlers()
            loop.run()
            return

        try:
            process_bulk_campaigns()
            self.stdout.write(self.style.SUCCESS('Successfully processed bulk campaigns'))
        except Exception as e:
            logger.error(f"Error processing bulk campaigns: {str(e)}")
            self.stdout.write(self.style.ERROR(f'Error processing bulk campaigns: {str(e)}'))
