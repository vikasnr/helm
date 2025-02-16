import requests
from typing import List

from helm.common.cache import CacheConfig
from helm.common.optional_dependencies import handle_module_not_found_error
from helm.common.request import wrap_request_time, Request, RequestResult, Sequence, Token
from helm.common.tokenization_request import (
    TokenizationRequest,
    TokenizationRequestResult,
)
from helm.proxy.tokenizers.tokenizer import Tokenizer
from .client import CachingClient, truncate_sequence

try:
    import vertexai
    from vertexai.language_models import TextGenerationModel, TextGenerationResponse  # PaLM2
    from vertexai.preview.generative_models import GenerativeModel, GenerationResponse, Candidate  # Gemini
except ModuleNotFoundError as e:
    handle_module_not_found_error(e, ["google"])


class VertexAIClient(CachingClient):
    """Client for Vertex AI models"""

    def __init__(
        self, tokenizer: Tokenizer, tokenizer_name: str, cache_config: CacheConfig, project_id: str, location: str
    ) -> None:
        super().__init__(cache_config=cache_config)
        self.project_id = project_id
        self.location = location
        self.tokenizer = tokenizer
        self.tokenizer_name = tokenizer_name

        vertexai.init(project=self.project_id, location=self.location)

    def make_request(self, request: Request) -> RequestResult:
        raise NotImplementedError


class VertexAITextClient(VertexAIClient):
    """Client for Vertex AI text models
    This client is used for PaLM2 for example."""

    def make_request(self, request: Request) -> RequestResult:
        """Make a request"""
        parameters = {
            "temperature": request.temperature,
            "max_output_tokens": request.max_tokens,
            "top_k": request.top_k_per_token,
            "top_p": request.top_p,
            "stop_sequences": request.stop_sequences,
            "candidate_count": request.num_completions,
            # TODO #2084: Add support for these parameters.
            # The parameters "echo", "frequency_penalty", and "presence_penalty" are supposed to be supported
            # in an HTTP request (See https://cloud.google.com/vertex-ai/docs/generative-ai/model-reference/text),
            # but they are not supported in the Python SDK:
            # https://github.com/googleapis/python-aiplatform/blob/beae48f63e40ea171c3f1625164569e7311b8e5a/vertexai/language_models/_language_models.py#L968C1-L980C1
            # "frequency_penalty": request.frequency_penalty,
            # "presence_penalty": request.presence_penalty,
            # "echo": request.echo_prompt,
        }

        completions: List[Sequence] = []
        model_name: str = request.model_engine

        try:

            def do_it():
                model = TextGenerationModel.from_pretrained(model_name)
                response = model.predict(request.prompt, **parameters)
                candidates: List[TextGenerationResponse] = response.candidates
                response_dict = {
                    "predictions": [{"text": completion.text for completion in candidates}],
                }  # TODO: Extract more information from the response
                return response_dict

            # We need to include the engine's name to differentiate among requests made for different model
            # engines since the engine name is not included in the request itself.
            # Same for the prompt.
            cache_key = CachingClient.make_cache_key(
                {
                    "engine": request.model_engine,
                    "prompt": request.prompt,
                    **parameters,
                },
                request,
            )

            response, cached = self.cache.get(cache_key, wrap_request_time(do_it))
        except (requests.exceptions.RequestException, AssertionError) as e:
            error: str = f"VertexAITextClient error: {e}"
            return RequestResult(success=False, cached=False, error=error, completions=[], embedding=[])

        for prediction in response["predictions"]:
            response_text = prediction["text"]

            # The Python SDK does not support echo
            # TODO #2084: Add support for echo.
            text: str = request.prompt + response_text if request.echo_prompt else response_text

            tokenization_result: TokenizationRequestResult = self.tokenizer.tokenize(
                TokenizationRequest(text, tokenizer=self.tokenizer_name)
            )

            # TODO #2085: Add support for log probs.
            # Once again, log probs seem to be supported by the API but not by the Python SDK.
            # HTTP Response body reference:
            # https://cloud.google.com/vertex-ai/docs/generative-ai/model-reference/text#response_body
            # Python SDK reference:
            # https://github.com/googleapis/python-aiplatform/blob/beae48f63e40ea171c3f1625164569e7311b8e5a/vertexai/language_models/_language_models.py#L868
            tokens: List[Token] = [
                Token(text=str(text), logprob=0, top_logprobs={}) for text in tokenization_result.raw_tokens
            ]

            completion = Sequence(text=response_text, logprob=0, tokens=tokens)
            sequence = truncate_sequence(completion, request, print_warning=True)
            completions.append(sequence)

        return RequestResult(
            success=True,
            cached=cached,
            request_time=response["request_time"],
            request_datetime=response["request_datetime"],
            completions=completions,
            embedding=[],
        )


class VertexAIChatClient(VertexAIClient):
    """Client for Vertex AI chat models
    This client is used for Gemini for example."""

    def make_request(self, request: Request) -> RequestResult:
        """Make a request"""
        parameters = {
            "temperature": request.temperature,
            "max_output_tokens": request.max_tokens,
            "top_k": request.top_k_per_token,
            "top_p": request.top_p,
            "stop_sequences": request.stop_sequences,
            "candidate_count": request.num_completions,
            # TODO #2084: Add support for these parameters.
            # The parameters "echo", "frequency_penalty", and "presence_penalty" are supposed to be supported
            # in an HTTP request (See https://cloud.google.com/vertex-ai/docs/generative-ai/model-reference/text),
            # but they are not supported in the Python SDK:
            # https://github.com/googleapis/python-aiplatform/blob/beae48f63e40ea171c3f1625164569e7311b8e5a/vertexai/language_models/_language_models.py#L968C1-L980C1
            # "frequency_penalty": request.frequency_penalty,
            # "presence_penalty": request.presence_penalty,
            # "echo": request.echo_prompt,
        }

        completions: List[Sequence] = []
        model_name: str = request.model_engine

        try:

            def do_it():
                model = GenerativeModel(model_name)

                # Here we differ from Vertex AI's tutotial.
                # https://cloud.google.com/vertex-ai/docs/generative-ai/multimodal/send-chat-prompts-gemini#send_chat_prompts   # noqa: E501
                # It would advise to use model.start_chat() but since we do not want to use Chat capabilities of
                # Vertex AI, we use model.generate_text() instead. Furthermore, chat.send_message() restricts the
                # output to only one candidate.
                # chat: ChatSession = model.start_chat()
                # See: https://github.com/googleapis/python-aiplatform/blob/e8c505751b10a9dc91ae2e0d6d13742d2abf945c/vertexai/generative_models/_generative_models.py#L812  # noqa: E501

                # TODO: Support VLM.
                # content can contain a list of Image, txt and more.
                # See: https://github.com/googleapis/python-aiplatform/blob/e8c505751b10a9dc91ae2e0d6d13742d2abf945c/vertexai/generative_models/_generative_models.py#L672C14-L672C14  # noqa: E501
                contents = request.prompt

                response: GenerationResponse = model.generate_content(contents, generation_config=parameters)
                candidates: List[Candidate] = response.candidates
                response_dict = {
                    "predictions": [{"text": completion.text for completion in candidates}],
                }  # TODO: Extract more information from the response
                return response_dict

            # We need to include the engine's name to differentiate among requests made for different model
            # engines since the engine name is not included in the request itself.
            # Same for the prompt.
            cache_key = CachingClient.make_cache_key(
                {
                    "model_name": model_name,
                    "prompt": request.prompt,
                    **parameters,
                },
                request,
            )

            response, cached = self.cache.get(cache_key, wrap_request_time(do_it))
        except (requests.exceptions.RequestException, AssertionError) as e:
            error: str = f"VertexAITextClient error: {e}"
            return RequestResult(success=False, cached=False, error=error, completions=[], embedding=[])

        for prediction in response["predictions"]:
            response_text = prediction["text"]

            # The Python SDK does not support echo
            # TODO #2084: Add support for echo.
            text: str = request.prompt + response_text if request.echo_prompt else response_text

            tokenization_result: TokenizationRequestResult = self.tokenizer.tokenize(
                TokenizationRequest(text, tokenizer=self.tokenizer_name)
            )

            # TODO #2085: Add support for log probs.
            tokens: List[Token] = [
                Token(text=str(text), logprob=0, top_logprobs={}) for text in tokenization_result.raw_tokens
            ]

            completion = Sequence(text=response_text, logprob=0, tokens=tokens)
            sequence = truncate_sequence(completion, request, print_warning=True)
            completions.append(sequence)

        return RequestResult(
            success=True,
            cached=cached,
            request_time=response["request_time"],
            request_datetime=response["request_datetime"],
            completions=completions,
            embedding=[],
        )
