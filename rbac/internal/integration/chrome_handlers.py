#
# Copyright 2019 Red Hat, Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#

"""Notification handlers of object change."""

import json
import logging
import os
from uuid import uuid4

from core.kafka import RBACProducer
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name
chrome_producer = RBACProducer()
chrome_topic = settings.EXTERNAL_CHROME_TOPIC

with open(os.path.join(settings.BASE_DIR, "internal", "integration", "chrome_message_template.json")) as template:
    message_template = json.load(template)


def build_chrome_message(event_type, uuid, org_id):
    """Create message based on template."""
    message = message_template
    message["id"] = str(uuid4())
    message["time"] = timezone.now().isoformat()
    message["data"]["organizations"] = [org_id]
    message["data"]["payload"]["entityType"] = "rbac.group"
    message["data"]["payload"]["entityId"] = str(uuid)
    message["data"]["payload"]["eventType"] = event_type
    return message


def send_chrome_message(event_type, uuid, org_id):
    """Build and send chrome message."""
    chrome_message = build_chrome_message(event_type, uuid, org_id)
    chrome_producer.send_kafka_message(chrome_topic, chrome_message)
