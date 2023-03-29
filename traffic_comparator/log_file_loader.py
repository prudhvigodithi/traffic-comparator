import json
import logging
import sys
from abc import ABC, abstractmethod
from enum import Enum
from pathlib import Path
from typing import Generator, List, Tuple, Type, Union

from traffic_comparator.data import Request, RequestResponsePair, Response

logger = logging.getLogger(__name__)


class UnknownLogFileFormatException(Exception):
    def __init__(self, format, original_exception) -> None:
        super().__init__(f"The log file format '{format}' is unknown or unsupported. "
                         f"Details: {str(original_exception)}")


class IncorrectLogFilePathInputException(Exception):
    def __init__(self, format, expected_number, actual_number) -> None:
        super().__init__(f"The incorrect number of log files for the format '{format}' were provided. "
                         f"{expected_number} files were expected but {actual_number} were provided.")


class LogFileFormat(Enum):
    HAPROXY_JSONS = "haproxy-jsons"
    REPLAYER_TRIPLES = "replayer-triples"


class BaseLogFileLoader(ABC):
    def __init__(self, log_file_paths: List[Path]) -> None:
        self.log_file_paths = log_file_paths

    @classmethod
    @abstractmethod
    def load(cls) -> Generator[Tuple[RequestResponsePair, RequestResponsePair], None, None]:
        pass


class ReplayerTriplesFileLoader(BaseLogFileLoader):
    """
    This is the log format output by the Replayer. Each line is a "triple": a json
    that contains the request, the response from the primary, and the response from the shadow.
    One idiosyncracy (for the time being) is that the headers are not in a seperate object -- they're
    mixed in with the main fields and therefore should be considered whatever fields are left over
    when the known ones are removed.

    {
      "request": {
        "Request-URI": XYZ,
        "Method": XYZ,
        "HTTP-Version": XYZ
        "body": XYZ,
        "header-1": XYZ,
        "header-2": XYZ

      },
      "primaryResponse": {
        "HTTP-Version": ABC,
        "Status-Code": ABC,
        "Reason-Phrase": ABC,
        "response_time_ms": 456, # milliseconds between the request and the response
        "body": ABC,
        "header-1": ABC
      },
      "shadowResponse": {
        "HTTP-Version": ABC,
        "Status-Code": ABC,
        "Reason-Phrase": ABC,
        "response_time_ms": 456, # milliseconds between the request and the response
        "body": ABC,
        "header-2": ABC
      }
    }
    The body field contains a string which can be decoded as json (or an empty string).
    """
    ignored_fields = ["Reason-Phrase", "HTTP-Version"]

    @classmethod
    def _parseBodyAsJson(cls, rawbody: str) -> Union[dict, str, None]:
        try:
            return json.loads(rawbody)
        except json.JSONDecodeError:
            logger.debug(f"Response body could not be parsed as JSON: {rawbody}")
        return rawbody

    @classmethod
    def _parseResponse(cls, responsedata) -> Response:
        r = Response()
        # Pull out known fields
        r.body = cls._parseBodyAsJson(responsedata.pop("body"))
        r.latency = responsedata.pop("response_time_ms")
        r.statuscode = int(responsedata.pop("Status-Code"))

        # Discard unnecessary fields
        for field in cls.ignored_fields:
            if field in responsedata:
                responsedata.pop(field)

        # The remaining fields are headers
        r.headers = responsedata
        return r

    @classmethod
    def _parseRequest(cls, requestdata) -> Request:
        r = Request()
        # Pull out known fields
        r.body = cls._parseBodyAsJson(requestdata.pop("body"))
        r.http_method = requestdata.pop("Method")
        r.uri = requestdata.pop("Request-URI")

        # Discard unnecessary fields
        for field in cls.ignored_fields:
            if field in requestdata:
                requestdata.pop(field)

        # The remaining fields are headers
        r.headers = requestdata
        return r

    @classmethod
    def _parseLine(cls, line) -> Tuple[RequestResponsePair, RequestResponsePair]:
        item = json.loads(line)

        # If any of these objects are missing, it will throw an error and this log
        # line will be skipped. The error is logged by the caller.
        requestdata = item['request']
        primaryResponseData = item['primaryResponse']
        shadowResponseData = item['shadowResponse']

        request = cls._parseRequest(requestdata)

        primaryPair = RequestResponsePair(request, cls._parseResponse(primaryResponseData))
        shadowPair = RequestResponsePair(request, cls._parseResponse(shadowResponseData),
                                         corresponding_pair=primaryPair)
        primaryPair.corresponding_pair = shadowPair

        return primaryPair, shadowPair

    @classmethod
    def load(cls) -> Generator[Tuple[RequestResponsePair, RequestResponsePair], None, None]:
        for line in sys.stdin:  # This line will wait indefinitely for input if there's no EOF
            try:
                yield cls._parseLine(line)
            except KeyError as e:
                logger.debug(f"Log file line was skipped due to parsing error. {e}")


LOG_FILE_LOADER_MAPPING: dict[LogFileFormat, Type[BaseLogFileLoader]] = {
    LogFileFormat.REPLAYER_TRIPLES: ReplayerTriplesFileLoader
}


def getLogFileLoader(logFileFormat: LogFileFormat) -> Type[BaseLogFileLoader]:
    try:
        return LOG_FILE_LOADER_MAPPING[logFileFormat]
    except KeyError as e:
        raise UnknownLogFileFormatException(logFileFormat, e)
