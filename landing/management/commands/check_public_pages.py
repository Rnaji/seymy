"""
Publication gate, run by build.sh before every deploy.

Renders the public pages (/, /privacy, /terms, /sms-test-consent) and fails while any
"À FOURNIR" placeholder remains, so a build with unfinished legal pages
never goes live: Render keeps serving the current deploy when a build fails.
"""
from django.core.management.base import BaseCommand, CommandError
from django.test import RequestFactory

from landing import public

PAGES = {
    '/': public.home,
    '/privacy': public.privacy,
    '/terms': public.terms,
    '/sms-test-consent': public.sms_test_consent,
}
MARKERS = ('data-todo', 'À FOURNIR')


class Command(BaseCommand):
    help = 'Fail if a public page still shows an "À FOURNIR" placeholder.'

    def handle(self, *args, **options):
        factory = RequestFactory()
        problems = []
        for path, view in PAGES.items():
            response = view(factory.get(path, secure=True))
            html = response.content.decode()
            if response.status_code != 200:
                problems.append(f'{path}: HTTP {response.status_code}')
            count = html.count('data-todo')
            if count or any(marker in html for marker in MARKERS):
                problems.append(f'{path}: {count} placeholder(s) left')
        if problems:
            raise CommandError('Public pages are not ready to publish: ' + '; '.join(problems))
        self.stdout.write(self.style.SUCCESS('Public pages ready: no placeholder left.'))
