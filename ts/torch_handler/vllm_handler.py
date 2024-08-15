import json
import logging
import pathlib
import time

from vllm import AsyncEngineArgs, AsyncLLMEngine, SamplingParams
from vllm.entrypoints.openai.protocol import (
    CompletionRequest,
    CompletionResponse,
    CompletionResponseChoice,
    CompletionResponseStreamChoice,
    CompletionStreamResponse,
    UsageInfo,
)
from vllm.entrypoints.openai.serving_completion import OpenAIServingCompletion
from vllm.entrypoints.openai.serving_engine import LoRAModulePath
from vllm.lora.request import LoRARequest

from ts.handler_utils.utils import send_intermediate_predict_response
from ts.torch_handler.base_handler import BaseHandler

logger = logging.getLogger(__name__)


class VLLMHandler(BaseHandler):
    def __init__(self):
        super().__init__()

        self.vllm_engine = None
        self.model_name = None
        self.model_dir = None
        self.lora_ids = {}
        self.adapters = None
        self.initialized = False

    def initialize(self, ctx):
        self.model_dir = ctx.system_properties.get("model_dir")
        vllm_engine_config = self._get_vllm_engine_config(
            ctx.model_yaml_config.get("handler", {})
        )

        self.vllm_engine = AsyncLLMEngine.from_engine_args(vllm_engine_config)

        self.adapters = ctx.model_yaml_config.get("handler", {}).get("adapters", {})
        lora_modules = [LoRAModulePath(n, p) for n, p in self.adapters.items()]

        try:
            served_model_name = vllm_engine_config.served_model_name
        except (KeyError, TypeError):
            served_model_name = [vllm_engine_config.model]

        self.completion_service = OpenAIServingCompletion(
            self.vllm_engine,
            vllm_engine_config,
            served_model_name,
            lora_modules=lora_modules,
            prompt_adapters=None,
            request_logger=None,
        )

        self.initialized = True

    async def handle(self, data, context):
        start_time = time.time()

        metrics = context.metrics

        data_preprocess = await self.preprocess(data, context)
        output = await self.inference(data_preprocess, context)
        output = await self.postprocess(output)

        stop_time = time.time()
        metrics.add_time(
            "HandlerTime", round((stop_time - start_time) * 1000, 2), None, "ms"
        )
        return output

    async def preprocess(self, requests, context):
        assert len(requests) == 1, "Expecting batch_size = 1"
        req_data = requests[0]
        data = req_data.get("data") or req_data.get("body")
        if isinstance(data, (bytes, bytearray)):
            data = data.decode("utf-8")

        return [self.prepare_completion_request(data)]

    async def inference(self, input_batch, context):
        logger.debug(f"Inputs: {input_batch[0]}")
        request, params, lora = input_batch[0]
        prompt = request.prompt
        stream = request.stream
        request_id = context.request_ids[0]
        generator = self.vllm_engine.generate(prompt, params, request_id, lora)
        request_header = context.get_all_request_header(0)
        use_openai = request_header.get("url_path", "").startswith("v1/completions")

        from unittest.mock import MagicMock

        raw_request = MagicMock()
        raw_request.headers = {}

        async def isd():
            return False

        raw_request.is_disconnected = isd
        g = await self.completion_service.create_completion(
            request,
            raw_request,
        )

        try:
            async for obj in g:
                print(f"{obj=}")
        except:
            print(f"{g.model_dump()=}")

        text_len = 0
        async for output in generator:
            if stream:
                if not use_openai:
                    result = {
                        "text": output.outputs[0].text[text_len:],
                        "tokens": output.outputs[0].token_ids[-1],
                    }
                    result = json.dumps(result)
                else:
                    choices = [
                        CompletionResponseStreamChoice(
                            index=0,
                            text=output.outputs[0].text[text_len:],
                            logprobs=None,
                            finish_reason=output.outputs[0].finish_reason,
                            stop_reason=output.outputs[0].stop_reason,
                        )
                    ]

                    usage = UsageInfo(
                        prompt_tokens=0,
                        completion_tokens=0,
                        total_tokens=0,
                    )

                    result = CompletionStreamResponse(
                        id=request_id,
                        created=int(time.time()),
                        model="model_name",
                        choices=choices,
                        usage=usage,
                    )
                    result = f"data: {result.model_dump_json()}\n\n"

                if not output.finished:
                    send_intermediate_predict_response(
                        [result], context.request_ids, "Result", 200, context
                    )
                text_len = len(output.outputs[0].text)

        if not stream:
            if not use_openai:
                result = {
                    "text": output.outputs[0].text,
                    "tokens": output.outputs[0].token_ids,
                }
                result = json.dumps(result)
            else:
                choices = [
                    CompletionResponseChoice(
                        index=0,
                        text=output.outputs[0].text,
                        logprobs=None,
                    )
                ]

                usage = UsageInfo(
                    prompt_tokens=0,
                    completion_tokens=0,
                    total_tokens=0,
                )

                result = CompletionResponse(
                    id=request_id,
                    created=int(time.time()),
                    model="model_name",
                    choices=choices,
                    usage=usage,
                )
                result = result.model_dump_json()

        return [result]

    async def postprocess(self, inference_outputs):
        return inference_outputs

    def prepare_completion_request(self, request_data):
        lora_request = self._get_lora_request(request_data)
        sampling_params = self._get_sampling_params(request_data)
        request = CompletionRequest.model_validate(request_data)
        return request, sampling_params, lora_request

    def _get_vllm_engine_config(self, handler_config: dict):
        vllm_engine_params = handler_config.get("vllm_engine_config", {})
        model = vllm_engine_params.get("model", {})
        if len(model) == 0:
            model_path = handler_config.get("model_path", {})
            assert (
                len(model_path) > 0
            ), "please define model in vllm_engine_config or model_path in handler"
            model = pathlib.Path(self.model_dir).joinpath(model_path)
            if not model.exists():
                logger.debug(
                    f"Model path ({model}) does not exist locally. Trying to give without model_dir as prefix."
                )
                model = model_path
            else:
                model = model.as_posix()
        logger.debug(f"EngineArgs model: {model}")
        vllm_engine_config = AsyncEngineArgs(model=model)
        self._set_attr_value(vllm_engine_config, vllm_engine_params)
        return vllm_engine_config

    def _get_sampling_params(self, req_data: dict):
        sampling_params = SamplingParams()
        self._set_attr_value(sampling_params, req_data)

        return sampling_params

    def _get_lora_request(self, req_data: dict):
        adapter_name = req_data.get("lora_adapter", "")

        if len(adapter_name) > 0:
            adapter_path = self.adapters.get(adapter_name, "")
            assert len(adapter_path) > 0, f"{adapter_name} misses adapter path"
            lora_id = self.lora_ids.setdefault(adapter_name, len(self.lora_ids) + 1)
            adapter_path = str(pathlib.Path(self.model_dir).joinpath(adapter_path))
            return LoRARequest(adapter_name, lora_id, adapter_path)

        return None

    def _set_attr_value(self, obj, config: dict):
        items = vars(obj)
        keys = list(config.keys())
        for k in keys:
            if k in items:
                setattr(obj, k, config[k])
                del config[k]
