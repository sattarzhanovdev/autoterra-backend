"""Keep email capabilities out of Django access/error log messages."""
import logging
import re


class EmailActionLogFilter(logging.Filter):
    def filter(self, record):
        message = record.getMessage()
        record.msg = re.sub(r'([?&]token=)[^\s&\"<>]*', r'\1[REDACTED]', message)
        record.args = ()
        return True


def install_log_filters():
    for name in ('django.server', 'django.request', 'gunicorn.access', 'gunicorn.error'):
        logger = logging.getLogger(name)
        if not any(isinstance(item, EmailActionLogFilter) for item in logger.filters):
            logger.addFilter(EmailActionLogFilter())
