import asyncio
import base64
import re
from io import BytesIO
from pathlib import Path
from typing import IO, List

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage
from megaparse_sdk.schema.document import BBOX, Block, Point2D, TextBlock
from megaparse_sdk.schema.document import Document as MPDocument
from megaparse_sdk.schema.extensions import FileExtension
from pdf2image import convert_from_path

from megaparse.parser import BaseParser
from megaparse.parser.entity import SupportedModel, TagEnum

# BASE_OCR_PROMPT = """
# Transcribe the content of this file into markdown. Be mindful of the formatting.
# Add formatting if you think it is not clear.
# Do not include page breaks and merge content of tables if it is continued in the next page.
# Add tags around what you identify as a table [TABLE], header - complete chain of characters that are repeated at each start of pages - [HEADER], table of content [TOC] in the format '[tag] ... [/tag]'
# Return only the parsed content.
# """

BASE_OCR_PROMPT = """
You are tasked with transcribing and formatting the content of a file into markdown. Your goal is to create a well-structured, readable markdown document that accurately represents the original content while adding appropriate formatting and tags.


Follow these instructions to complete the task:

1. Carefully read through the entire file content.

2. Transcribe the content into markdown format, paying close attention to the existing formatting and structure.

3. If you encounter any unclear formatting in the original content, use your judgment to add appropriate markdown formatting to improve readability and structure.

4. For tables, headers, and table of contents, add the following tags:
   - Tables: Enclose the entire table in [TABLE] and [/TABLE] tags. Merge content of tables if it is continued in the next page.
   - Headers (complete chain of characters repeated at the start of each page): Enclose in [HEADER] and [/HEADER] tags inside the markdown file.
   - Table of contents: Enclose in [TOC] and [/TOC] tags

5. When transcribing tables:
   - If a table continues across multiple pages, merge the content into a single, cohesive table.
   - Use proper markdown table formatting with pipes (|) and hyphens (-) for table structure.

6. Do not include page breaks in your transcription.

7. Maintain the logical flow and structure of the document, ensuring that sections and subsections are properly formatted using markdown headers (# for main headers, ## for subheaders, etc.).

8. Use appropriate markdown syntax for other formatting elements such as bold, italic, lists, and code blocks as needed.

10. Return only the parsed content in markdown format, including the specified tags for tables, headers, and table of contents.
"""


class MegaParseVision(BaseParser):
    supported_extensions = [FileExtension.PDF]

    def __init__(self, model: BaseChatModel, **kwargs):
        if hasattr(model, "model_name"):
            if not SupportedModel.is_supported(model.model_name):
                raise ValueError(
                    f"Invald model name, MegaParse vision only supports model that have vision capabilities. "
                    f"{model.model_name} is not supported."
                )
        self.model = model

        self.parsed_chunks: list[str] | None = None

    def process_file(self, file_path: str, image_format: str = "PNG") -> List[str]:
        """
        Process a PDF file and convert its pages to base64 encoded images.

        :param file_path: Path to the PDF file
        :param image_format: Format to save the images (default: PNG)
        :return: List of base64 encoded images
        """
        try:
            images = convert_from_path(file_path)
            images_base64 = []
            for image in images:
                buffered = BytesIO()
                image.save(buffered, format=image_format)
                image_base64 = base64.b64encode(buffered.getvalue()).decode("utf-8")
                images_base64.append(image_base64)
            return images_base64
        except Exception as e:
            raise ValueError(f"Error processing PDF file: {str(e)}")

    def get_element(self, tag: TagEnum, chunk: str):
        pattern = rf"\[{tag.value}\]([\s\S]*?)\[/{tag.value}\]"
        all_elmts = re.findall(pattern, chunk)
        if not all_elmts:
            print(f"No {tag.value} found in the chunk")
            return []
        return [elmt.strip() for elmt in all_elmts]

    async def asend_to_mlm(self, images_data: List[str]) -> str:
        """
        Send images to the language model for processing.

        :param images_data: List of base64 encoded images
        :return: Processed content as a string
        """
        images_prompt = [
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{image_data}"},
            }
            for image_data in images_data
        ]
        message = HumanMessage(
            content=[
                {"type": "text", "text": BASE_OCR_PROMPT},
                *images_prompt,
            ],
        )
        response = await self.model.ainvoke([message])
        return str(response.content)

    def send_to_mlm(self, images_data: List[str]) -> str:
        """
        Send images to the language model for processing.

        :param images_data: List of base64 encoded images
        :return: Processed content as a string
        """
        images_prompt = [
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{image_data}"},
            }
            for image_data in images_data
        ]
        message = HumanMessage(
            content=[
                {"type": "text", "text": BASE_OCR_PROMPT},
                *images_prompt,
            ],
        )
        response = self.model.invoke([message])
        return str(response.content)

    async def aconvert(
        self,
        file_path: str | Path | None = None,
        file: IO[bytes] | None = None,
        file_extension: FileExtension | None = None,
        batch_size: int = 3,
        **kwargs,
    ) -> MPDocument:
        """
        Parse a PDF file and process its content using the language model.

        :param file_path: Path to the PDF file
        :param batch_size: Number of pages to process concurrently
        :return: List of processed content strings
        """
        if not file_path:
            raise ValueError("File_path should be provided to run MegaParseVision")

        if isinstance(file_path, Path):
            file_path = str(file_path)

        self.check_supported_extension(file_extension, file_path)

        pdf_base64 = self.process_file(file_path)
        n_pages = len(pdf_base64)
        tasks = [
            self.asend_to_mlm(pdf_base64[i : i + batch_size])
            for i in range(0, len(pdf_base64), batch_size)
        ]
        self.parsed_chunks = await asyncio.gather(*tasks)
        responses = self.get_cleaned_content("\n".join(self.parsed_chunks))
        return self.__to_elements_list__(responses, n_pages=n_pages)

    def convert(
        self,
        file_path: str | Path | None = None,
        file: IO[bytes] | None = None,
        file_extension: FileExtension | None = None,
        batch_size: int = 3,
        **kwargs,
    ) -> MPDocument:
        """
        Parse a PDF file and process its content using the language model.

        :param file_path: Path to the PDF file
        :param batch_size: Number of pages to process at a time
        :return: List of processed content strings
        """
        if not file_path:
            raise ValueError("File_path should be provided to run MegaParseVision")

        if isinstance(file_path, Path):
            file_path = str(file_path)

        self.check_supported_extension(file_extension, file_path)

        pdf_base64 = self.process_file(file_path)
        n_pages = len(pdf_base64)
        chunks = [
            pdf_base64[i : i + batch_size]
            for i in range(0, len(pdf_base64), batch_size)
        ]
        self.parsed_chunks = []
        for chunk in chunks:
            response = self.send_to_mlm(chunk)
            self.parsed_chunks.append(response)
        responses = self.get_cleaned_content("\n".join(self.parsed_chunks))
        return self.__to_elements_list__(responses, n_pages)

    def get_cleaned_content(self, parsed_file: str) -> str:
        """
        Get cleaned parsed file without any tags defined in TagEnum.

        This method removes all tags from TagEnum from the parsed file, formats the content,
        and handles the HEADER tag specially by keeping only the first occurrence.

        Args:
            parsed_file (str): The parsed file content with tags.

        Returns:
            str: The cleaned content without TagEnum tags.

        """
        tag_pattern = "|".join(map(re.escape, TagEnum.__members__.values()))
        tag_regex = rf"\[({tag_pattern})\](.*?)\[/\1\]"
        # handle the HEADER tag specially
        header_pattern = rf"\[{TagEnum.HEADER.value}\](.*?)\[/{TagEnum.HEADER.value}\]"
        headers = re.findall(header_pattern, parsed_file, re.DOTALL)
        if headers:
            first_header = headers[0].strip()
            # Remove all HEADER tags and their content
            parsed_file = re.sub(header_pattern, "", parsed_file, flags=re.DOTALL)
            # Add the first header back at the beginning
            parsed_file = f"{first_header}\n{parsed_file}"

        # Remove all other tags
        def remove_tag(match):
            return match.group(2)

        cleaned_content = re.sub(tag_regex, remove_tag, parsed_file, flags=re.DOTALL)

        cleaned_content = re.sub(r"^```.*$\n?", "", cleaned_content, flags=re.MULTILINE)
        cleaned_content = re.sub(r"\n\s*\n", "\n\n", cleaned_content)
        cleaned_content = cleaned_content.replace("|\n\n|", "|\n|")
        cleaned_content = cleaned_content.strip()

        return cleaned_content

    def __to_elements_list__(self, mpv_doc: str, n_pages: int) -> MPDocument:
        list_blocks: List[Block] = [
            TextBlock(
                text=mpv_doc,
                metadata={},
                page_range=(0, n_pages - 1),
                bbox=BBOX(top_left=Point2D(x=0, y=0), bottom_right=Point2D(x=1, y=1)),
            )
        ]
        return MPDocument(
            metadata={},
            detection_origin="megaparse_vision",
            content=list_blocks,
        )
