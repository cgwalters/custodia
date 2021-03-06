# Copyright (C) 2015  Custodia Project Contributors - see LICENSE file

from custodia.message.common import InvalidMessage
from custodia.message.common import MessageHandler
from six import string_types
import json


class SimpleKey(MessageHandler):
    """Handles 'simple' messages"""

    def parse(self, msg, name):
        """Parses a simple message

        :param req: ignored
        :param msg: the json-decoded value

        :raises UnknownMessageType: if the type is not 'simple'
        :raises InvalidMessage: if the message cannot be parsed or validated
        """

        # On requests we imply 'simple' if there is no input message
        if msg is None:
            return

        if not isinstance(msg, string_types):
            raise InvalidMessage("The 'value' attribute is not a string")

        self.payload = msg

    def reply(self, output):
        return json.dumps({'type': 'simple', 'value': output},
                          separators=(',', ':'))
