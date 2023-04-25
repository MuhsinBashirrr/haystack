from typing import List, Optional, Dict, Union, Tuple

import time
import logging
from pathlib import Path
import subprocess
from html.parser import HTMLParser

import requests

from haystack.nodes.file_converter.base import BaseConverter
from haystack.schema import Document


logger = logging.getLogger(__name__)


try:
    from tika import parser as tika_parser
except ImportError as exc:
    logger.debug(
        "tika could not be imported. "
        "Run 'pip install farm-haystack[file-conversion]' or 'pip install tika' to fix this issue."
    )
    tika_parser = None


TIKA_CONTAINER_NAME = "tika"


def launch_tika(sleep=15, delete_existing=False):
    # Start a Tika server via Docker

    logger.debug("Starting Tika ...")
    # This line is needed since it is not possible to start a new docker container with the name tika if there is a stopped image with the same name
    # docker rm only succeeds if the container is stopped, not if it is running
    if delete_existing:
        _ = subprocess.run([f"docker rm --force {TIKA_CONTAINER_NAME}"], shell=True, stdout=subprocess.DEVNULL)
    status = subprocess.run(
        [
            f"docker start {TIKA_CONTAINER_NAME} > /dev/null 2>&1 || docker run -p 9998:9998  --name {TIKA_CONTAINER_NAME} apache/tika:1.28.4"
        ],
        shell=True,
    )
    if status.returncode:
        logger.warning(
            "Tried to start Tika through Docker but this failed. "
            "It is likely that there is already an existing Tika instance running. "
        )
    else:
        time.sleep(sleep)


class TikaXHTMLParser(HTMLParser):
    # Use the built-in HTML parser with minimum dependencies
    def __init__(self):
        self.ingest = True
        self.page = ""
        self.pages: List[str] = []
        super(TikaXHTMLParser, self).__init__()

    def handle_starttag(self, tag, attrs):
        # find page div
        pagediv = [value for attr, value in attrs if attr == "class" and value == "page"]
        if tag == "div" and pagediv:
            self.ingest = True

    def handle_endtag(self, tag):
        # close page div, or a single page without page div, save page and open a new page
        if (tag == "div" or tag == "body") and self.ingest:
            self.ingest = False
            # restore words hyphened to the next line
            self.pages.append(self.page.replace("-\n", ""))
            self.page = ""

    def handle_data(self, data):
        if self.ingest:
            self.page += data


class TikaConverter(BaseConverter):
    def __init__(
        self,
        tika_url: str = "http://localhost:9998/tika",
        remove_numeric_tables: bool = False,
        valid_languages: Optional[List[str]] = None,
        id_hash_keys: Optional[List[str]] = None,
        timeout: Union[float, Tuple[float, float]] = 10.0,
    ):
        """
        :param tika_url: URL of the Tika server
        :param remove_numeric_tables: This option uses heuristics to remove numeric rows from the tables.
                                      The tabular structures in documents might be noise for the reader model if it
                                      does not have table parsing capability for finding answers. However, tables
                                      may also have long strings that could possible candidate for searching answers.
                                      The rows containing strings are thus retained in this option.
        :param valid_languages: validate languages from a list of languages specified in the ISO 639-1
                                (https://en.wikipedia.org/wiki/ISO_639-1) format.
                                This option can be used to add test for encoding errors. If the extracted text is
                                not one of the valid languages, then it might likely be encoding error resulting
                                in garbled text.
        :param id_hash_keys: Generate the document id from a custom list of strings that refer to the document's
            attributes. If you want to ensure you don't have duplicate documents in your DocumentStore but texts are
            not unique, you can modify the metadata and pass e.g. `"meta"` to this field (e.g. [`"content"`, `"meta"`]).
            In this case the id will be generated by using the content and the defined metadata.
        :param timeout: How many seconds to wait for the server to send data before giving up,
            as a float, or a :ref:`(connect timeout, read timeout) <timeouts>` tuple.
            Defaults to 10 seconds.
        """
        if not tika_parser:
            raise ImportError(
                "tika could not be imported. "
                "Run 'pip install farm-haystack[file-conversion]' or 'pip install tika' to fix this issue."
            )
        super().__init__(
            remove_numeric_tables=remove_numeric_tables, valid_languages=valid_languages, id_hash_keys=id_hash_keys
        )

        ping = requests.get(tika_url, timeout=timeout)
        if ping.status_code != 200:
            raise Exception(
                f"Apache Tika server is not reachable at the URL '{tika_url}'. To run it locally"
                f"with Docker, execute: 'docker run -p 9998:9998 apache/tika:1.28.4'"
            )
        self.tika_url = tika_url
        super().__init__(remove_numeric_tables=remove_numeric_tables, valid_languages=valid_languages)

    def convert(
        self,
        file_path: Path,
        meta: Optional[Dict[str, str]] = None,
        remove_numeric_tables: Optional[bool] = None,
        valid_languages: Optional[List[str]] = None,
        encoding: Optional[str] = None,
        id_hash_keys: Optional[List[str]] = None,
    ) -> List[Document]:
        """
        :param file_path: path of the file to convert
        :param meta: dictionary of meta data key-value pairs to append in the returned document.
        :param remove_numeric_tables: This option uses heuristics to remove numeric rows from the tables.
                                      The tabular structures in documents might be noise for the reader model if it
                                      does not have table parsing capability for finding answers. However, tables
                                      may also have long strings that could possible candidate for searching answers.
                                      The rows containing strings are thus retained in this option.
        :param valid_languages: validate languages from a list of languages specified in the ISO 639-1
                                (https://en.wikipedia.org/wiki/ISO_639-1) format.
                                This option can be used to add test for encoding errors. If the extracted text is
                                not one of the valid languages, then it might likely be encoding error resulting
                                in garbled text.
        :param encoding: Not applicable
        :param id_hash_keys: Generate the document id from a custom list of strings that refer to the document's
            attributes. If you want to ensure you don't have duplicate documents in your DocumentStore but texts are
            not unique, you can modify the metadata and pass e.g. `"meta"` to this field (e.g. [`"content"`, `"meta"`]).
            In this case the id will be generated by using the content and the defined metadata.

        :return: A list of pages and the extracted meta data of the file.
        """
        if remove_numeric_tables is None:
            remove_numeric_tables = self.remove_numeric_tables
        if valid_languages is None:
            valid_languages = self.valid_languages
        if id_hash_keys is None:
            id_hash_keys = self.id_hash_keys

        parsed = tika_parser.from_file(file_path.as_posix(), self.tika_url, xmlContent=True)
        parser = TikaXHTMLParser()
        parser.feed(parsed["content"])

        cleaned_pages = []
        # TODO investigate title of document appearing in the first extracted page
        for page in parser.pages:
            lines = page.splitlines()
            cleaned_lines = []
            for line in lines:
                words = line.split()
                digits = [word for word in words if any(i.isdigit() for i in word)]

                # remove lines having > 40% of words as digits AND not ending with a period(.)
                if remove_numeric_tables:
                    if words and len(digits) / len(words) > 0.4 and not line.strip().endswith("."):
                        logger.debug("Removing line '%s' from %s", line, file_path)
                        continue

                cleaned_lines.append(line)

            page = "\n".join(cleaned_lines)
            cleaned_pages.append(page)

        if valid_languages:
            document_text = "".join(cleaned_pages)
            if not self.validate_language(document_text, valid_languages):
                logger.warning(
                    "The language for %s is not one of %s. The file may not have "
                    "been decoded in the correct text format.",
                    file_path,
                    valid_languages,
                )

        text = "\f".join(cleaned_pages)
        document = Document(content=text, meta={**parsed["metadata"], **(meta or {})}, id_hash_keys=id_hash_keys)
        return [document]
