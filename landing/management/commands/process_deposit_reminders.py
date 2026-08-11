from django.core.management.base import BaseCommand

from landing.delivery import process_deposit_reminders


class Command(BaseCommand):
    help = (
        'Send due automatic deposit reminders (day +2 / day +5) for unpaid '
        'Seymy payment requests. Demo delivery logs messages; no live SMS.'
    )

    def handle(self, *args, **options):
        result = process_deposit_reminders()
        self.stdout.write(
            self.style.SUCCESS(
                f"Deposit reminders: day2={result['sent_day2']} "
                f"day5={result['sent_day5']} skipped={result['skipped']}"
            )
        )
