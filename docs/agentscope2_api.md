# AgentScope 2.0.9 — Code-Level API Cookbook (migration from 1.0)

Every statement below is derived from reading the actual source under
`/tmp/as209/extracted/agentscope` (package `agentscope`, `__version__ = "2.0.9"`,
file `_version.py`). Class names, constructor argument names and signatures are
copied verbatim. Where behaviour is documented with a snippet, the snippet is
taken from / modelled on the real source.

> **The single most important structural fact:** 2.0 replaces the 1.0
> `ReActAgent` + `Memory` + string-content `Msg` stack with **one `Agent` class**
> driven by a pydantic **`AgentState`** and a list of typed **content blocks**.
> There is no `Memory` object: the whole conversation, compressed summary, tool
> cache, task list and permission rules live on `Agent.state` (`AgentState`), and
> every message is `Msg(content: list[ContentBlock])`. Model output is streamed as
> typed **`AgentEvent`s** (`Agent.reply_stream()`), and human-in-the-loop is a
> first-class pause/resume protocol on those events.

---

## 1. Package layout & top-level imports

Top-level `agentscope/__init__.py` exports only:

```python
from agentscope import logger, setup_logger, set_id_factory, set_timestamp_factory, __version__
```

Sub-packages (each has an `__init__.py` re-exporting its public objects):

| Sub-package | Purpose |
|---|---|
| `agentscope.agent` | `Agent`, `A2AAgent`, `RealtimeAgent`, config classes |
| `agentscope.model` | `ChatModelBase`, per-provider chat models, `ChatResponse`, `ModelCard` |
| `agentscope.credential` | `CredentialBase`, per-provider credentials, `CredentialFactory` |
| `agentscope.message` | `Msg`, `UserMsg`/`AssistantMsg`/`SystemMsg`, content blocks |
| `agentscope.tool` | `Toolkit`, `ToolBase`, `FunctionTool`, builtins |
| `agentscope.formatter` | `FormatterBase` + per-provider formatters |
| `agentscope.middleware` | `MiddlewareBase` + optional middlewares |
| `agentscope.permission` | `PermissionMode/Behavior/Rule/Decision/Engine/Context` |
| `agentscope.state` | `AgentState`, `TaskContext`, `ReplyContext`, `ToolContext`, `Task` |
| `agentscope.event` | `AgentEvent` union + all event classes |
| `agentscope.pipeline` | `PipelineProtocol`, `TeamPipeline`, `GoalPipeline` |
| `agentscope.types` | `ReplyFinishedReason`, `ErrorInfo`, `ErrorType`, hooks |
| `agentscope.mcp` | `MCPClient`, `StdioMCPConfig`, `HttpMCPConfig` |
| `agentscope.skill` | `Skill`, `SkillLoaderBase`, `LocalSkillLoader` |
| `agentscope.embedding` / `rag` / `tts` / `realtime` / `sop` / `classifier` / `tui` / `console` | supporting systems |
| `agentscope.workspace` | `Offloader`, `WorkspaceBase`, sandbox backends |
| `agentscope.exception` | error hierarchy (`AgentOrientedException`, `ToolNotFoundError`, …) |
| `agentscope.app` | the full HTTP application layer (router/service/storage/hub) — **not needed for a library embed** |

Typical 1.0→2.0 import rewrite:

```python
from agentscope.agent import Agent, ReActConfig, ContextConfig, ModelConfig, InjectionConfig
from agentscope.model import OpenAIChatModel
from agentscope.credential import OpenAICredential
from agentscope.message import Msg, UserMsg, AssistantMsg, SystemMsg, TextBlock, ToolCallBlock
from agentscope.tool import Toolkit, FunctionTool, ToolResponse, ToolChunk
from agentscope.formatter import OpenAIChatFormatter
from agentscope.event import (ReplyStartEvent, ReplyEndEvent, TextBlockDeltaEvent,
                              ToolCallEndEvent, ToolResultEndEvent,
                              RequireUserConfirmEvent, UserConfirmResultEvent, ConfirmResult)
from agentscope.permission import PermissionMode, PermissionBehavior, PermissionRule, PermissionDecision
```

---

## 2. Messages & blocks (`agentscope.message`)

`agentscope/message/__init__.py` exports:
`TextBlock`, `ThinkingBlock`, `HintBlock`, `ToolCallBlock`, `ToolCallState`,
`ToolResultBlock`, `ToolResultState`, `DataBlock`, `Base64Source`, `URLSource`,
`ContentBlock`, `ContentBlockTypes`, `Msg`, `UserMsg`, `AssistantMsg`,
`SystemMsg`, `Usage`.

### 2.1 `Msg` — a pydantic `BaseModel` (not a dataclass)

```python
class Msg(BaseModel):
    name: str
    content: list[ContentBlock]
    role: Literal["user", "assistant", "system"]
    id: str = Field(default_factory=_generate_id)
    metadata: dict = Field(default_factory=dict)
    created_at: str = Field(default_factory=_generate_timestamp)
    usage: Usage | None = None
    finished_at: str | None = None
    finished_reason: ReplyFinishedReason | None = None
    structured_output: dict | None = None
    error: ErrorInfo | None = None
```

Key points:

* `content` is **always a list of blocks**, never a plain string.
* A `@model_validator(mode="after")` enforces role/content rules: `role="user"`
  may hold only `TextBlock`/`DataBlock`; `role="system"` only `TextBlock`;
  `role="assistant"` anything.
* Because it is a pydantic model, `Msg(...)` is **keyword-only**. Use the factory
  functions for positional construction.

### 2.2 Factory functions `UserMsg` / `AssistantMsg` / `SystemMsg`

These are **module-level functions returning `Msg`**, not classes. A plain string
is auto-wrapped in a `TextBlock` (`_to_blocks`).

```python
def UserMsg(name: str, content: str | list[TextBlock | DataBlock],
            metadata: dict | None = None, created_at: str | None = None,
            finished_at: str | None = None,
            finished_reason: ReplyFinishedReason | None = None,
            id: str | None = None) -> Msg

def AssistantMsg(name: str, content: str | list[ContentBlock],
                 metadata: dict | None = None, created_at: str | None = None,
                 finished_at: str | None = None,
                 finished_reason: ReplyFinishedReason | None = None,
                 structured_output: dict | None = None,
                 id: str | None = None, usage: Usage | None = None) -> Msg

def SystemMsg(name: str, content: str | list[TextBlock],
              metadata: dict | None = None, created_at: str | None = None,
              finished_at: str | None = None,
              finished_reason: ReplyFinishedReason | None = None,
              id: str | None = None) -> Msg
```

```python
msg = UserMsg("user", "hello")                       # positional
msg = AssistantMsg("agent", [TextBlock(text="hi")])  # block list
msg = Msg(name="agent", role="assistant", content=[TextBlock(text="hi")])  # kwargs only
```

### 2.3 Reading text / blocks out of a `Msg`

```python
def has_content_blocks(self, block_type: ContentBlockTypes | list[ContentBlockTypes] | None = None) -> bool
def get_text_content(self, separator: str = "\n") -> str | None   # concatenates TextBlocks; None if none
def get_content_blocks(self, block_type: ContentBlockTypes | List[ContentBlockTypes] | None = None) -> Sequence[ContentBlock]
def append_event(self, event: AgentEvent) -> Self                 # applies a streaming event in place
def append_usage(self, usage: Usage) -> Self
```

```python
text   = msg.get_text_content() or ""
calls  = msg.get_content_blocks("tool_call")     # -> list[ToolCallBlock]
calls  = msg.get_content_blocks(["tool_call", "tool_result"])
```

`Msg.append_event(event)` is what turns a stream of `AgentEvent`s into a
persisted `Msg` (the console renderer uses it: `self._msg.append_event(event)`).

### 2.4 Block classes

All blocks derive from `BaseModel`. Common fields `id`, `created_at`,
`finished_at` are auto-generated.

| Block | `type` | Payload fields |
|---|---|---|
| `TextBlock` | `"text"` | `text: str` |
| `ThinkingBlock` | `"thinking"` | `thinking: str`, `model_config = ConfigDict(extra="allow")` (carries provider extras like `signature`) |
| `HintBlock` | `"hint"` | `hint: str \| list[TextBlock \| DataBlock]`, `source: str \| None` |
| `ToolCallBlock` | `"tool_call"` | `id`, `name`, `input: str` (raw JSON accumulated over streaming), `state: ToolCallState`, `suggested_rules: list[PermissionRule]` |
| `ToolResultBlock` | `"tool_result"` | `id`, `name`, `output: str \| list[TextBlock \| DataBlock]`, `state: ToolResultState`, `metadata: dict` |
| `DataBlock` | `"data"` | `id`, `source: Base64Source \| URLSource`, `name: str \| None` |

There is **no separate image/audio/video block class** — multimodal content is a
single `DataBlock` whose `source.media_type` is e.g. `image/png`, `audio/wav`,
`video/mp4`.

```python
class Base64Source(BaseModel):   # type="base64"
    data: str; media_type: str
class URLSource(BaseModel):      # type="url"
    url: AnyUrl; media_type: str
```

```python
ContentBlock = TextBlock | ThinkingBlock | HintBlock | ToolCallBlock | ToolResultBlock | DataBlock
ContentBlockTypes = Literal["text","thinking","hint","tool_call","tool_result","data"]
```

### 2.5 Enums

```python
class ToolCallState(StrEnum):
    PENDING = "pending"      # initial, not yet processed by permission
    ASKING = "asking"        # waiting for user confirmation
    ALLOWED = "allowed"      # allowed, waiting for execution
    SUBMITTED = "submitted"  # external tool submitted, waiting for result
    FINISHED = "finished"

class ToolResultState(StrEnum):
    SUCCESS = "success"; ERROR = "error"; INTERRUPTED = "interrupted"
    DENIED = "denied"; RUNNING = "running"
```

`ToolCallBlock` and `ToolResultBlock` use `model_config = ConfigDict(use_enum_values=True)`,
so `.state` serialises as the bare string; `Msg.append_event` coerces it back.

### 2.6 `Usage`

```python
class Usage(BaseModel):
    input_tokens: int
    output_tokens: int
    cache_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
```

---

## 3. Models (`agentscope.model`) + credentials (`agentscope.credential`)

`agentscope/model/__init__.py` exports: `ChatUsage`, `ChatModelBase`,
`ChatResponse`, `FinishedReason`, `ModelCard`, `StructuredResponse`,
`AnthropicChatModel`, `DashScopeChatModel`, `DeepSeekChatModel`,
`GeminiChatModel`, `MiniMaxChatModel`, `OllamaChatModel`, `OpenAIChatModel`,
`XAIChatModel`, `MoonshotChatModel`, `OpenAIResponseModel`,
`VolcengineChatModel`.

### 3.1 `ChatModelBase`

```python
class ChatModelBase:
    credential: CredentialBase
    model: str
    stream: bool
    max_retries: int
    retry_delay: float
    context_size: int

    class Parameters(BaseModel):
        """Subclasses override this inner class."""

    def __init__(self,
                 credential: CredentialBase,
                 model: str,
                 parameters: BaseModel,
                 stream: bool = True,
                 max_retries: int = 3,
                 retry_delay: float = 1.0,
                 context_size: int = 32768) -> None: ...

    async def __call__(self,
                       messages: list[Msg],
                       tools: list[dict] | None = None,
                       tool_choice: ToolChoice | None = None,
                       **kwargs) -> ChatResponse | AsyncGenerator[ChatResponse, None]: ...

    async def count_tokens(self, messages: list[Msg], tools: list[dict] | None) -> int: ...

    async def generate_structured_output(self,
                                         messages: list[Msg],
                                         structured_model: Type[BaseModel] | dict,
                                         **kwargs) -> StructuredResponse: ...
```

### 3.2 `OpenAIChatModel` (the one to migrate to)

```python
class OpenAIChatModel(ChatModelBase):
    type: Literal["openai_chat"] = "openai_chat"

    class Parameters(BaseModel):
        max_tokens: int | None = Field(default=None, gt=0)
        thinking_enable: bool = Field(default=False)
        reasoning_effort: Literal["none","minimal","low","medium","high","xhigh"] | None = None
        temperature: float | None = Field(default=None, ge=0, le=2)
        top_p: float | None = Field(default=None, gt=0, le=1)
        parallel_tool_calls: bool = Field(default=True)
        voice: str | None = None            # omni/audio models; setting it turns on audio output

    def __init__(self,
                 credential: OpenAICredential,
                 model: str,
                 parameters: OpenAIChatModel.Parameters | None = None,
                 stream: bool = True,
                 max_retries: int = 3,
                 retry_delay: float = 1.0,
                 context_size: int = 128000,
                 formatter: FormatterBase | None = None,
                 client_kwargs: dict[str, Any] | None = None,
                 extra_body: dict[str, Any] | None = None) -> None: ...
```

* `formatter` defaults to `OpenAIChatFormatter()`; it is stored on the **model**
  (`self.formatter`) and used inside `_call_api` (`await self.formatter.format(messages)`).
* `extra_body` is a constructor kwarg (not a `Parameters` field); it is sent as
  the request `extra_body`.
* `client_kwargs` are forwarded to `openai.AsyncClient` (e.g. `timeout`,
  `default_headers`, `http_client`).
* `base_url`/`organization` come from the credential, not the model.
* Retries are on `openai.APIConnectionError/APITimeoutError/RateLimitError/InternalServerError`.

```python
from agentscope.model import OpenAIChatModel
from agentscope.credential import OpenAICredential
from agentscope.formatter import OpenAIChatFormatter

model = OpenAIChatModel(
    credential=OpenAICredential(
        api_key="sk-...",                 # SecretStr
        base_url="https://api.openai.com/v1",
        organization=None,
    ),
    model="gpt-4.1",
    parameters=OpenAIChatModel.Parameters(temperature=0.7, max_tokens=2048),
    stream=True,
    context_size=128000,
    formatter=OpenAIChatFormatter(),
    extra_body={"some_provider_flag": True},
)
```

### 3.3 Credentials

`agentscope/credential/__init__.py`: `CredentialBase`, `AnthropicCredential`,
`DashScopeCredential`, `DeepSeekCredential`, `GeminiCredential`,
`MiniMaxCredential`, `MoonshotCredential`, `OllamaCredential`,
`OpenAICredential`, `TypeSafeCredential`, `XAICredential`,
`VolcengineCredential`, `CredentialFactory`.

`OpenAICredential` lives in `agentscope/credential/_openai.py`:

```python
class OpenAICredential(CredentialBase):
    type: Literal["openai_credential"] = "openai_credential"
    api_key: SecretStr = Field(description="The OpenAI API key.")
    organization: str | None = Field(default=None)
    base_url: str | None = Field(default=None)
```

`CredentialBase` (pydantic `BaseModel`) adds `id: str` and `name: str = ""`, plus
class methods `get_chat_model_class()`, `get_embedding_model_class()`,
`get_tts_model_classes()`, `get_realtime_model_classes()`, `list_models()`.

Note `api_key` is a `SecretStr`: read it with `.get_secret_value()`.

### 3.4 Calling the model

```python
res = await model(messages, tools=None, tool_choice=None)
# stream=True  -> res is an AsyncGenerator[ChatResponse, None]; the base __call__
#                 accumulates deltas and, after the last chunk, yields a final
#                 ChatResponse(is_last=True) whose .content is the complete reply.
# stream=False -> res is a single ChatResponse(is_last=True).
for block in res.content:  # TextBlock | ThinkingBlock | ToolCallBlock | DataBlock
    ...
```

`ChatResponse` / `StructuredResponse` / `FinishedReason` live in
`model/_model_response.py`:

```python
@dataclass
class ChatResponse(DictMixin):
    content: List[TextBlock | ToolCallBlock | ThinkingBlock | DataBlock]
    is_last: bool
    id: str; created_at: str; type: Literal["chat_response"]
    usage: ChatUsage | None
    finished_reason: FinishedReason   # COMPLETED | INTERRUPTED
    metadata: dict
    # helpers: append_text, append_thinking, append_tool_call, append_data_block,
    #          append_chat_response

@dataclass
class StructuredResponse:
    content: dict          # the validated structured output
    id / created_at / type="structured_response" / usage / metadata / finished_reason

@dataclass
class ChatUsage(DictMixin):
    input_tokens: int; output_tokens: int; time: float
    cache_creation_input_tokens: int = 0; cache_input_tokens: int = 0
```

`count_tokens(messages, tools)` is a **fast byte/4 estimate** (plus 2000 tokens
per `DataBlock`); subclasses may override. (This replaces 1.0's `OpenAITokenCounter`.)

`generate_structured_output(messages, structured_model, **kwargs)` forces a tool
call named `generate_structured_output`, validates against the pydantic model
(or JSON-schema dict) and returns a `StructuredResponse`. It tries a strategy
ladder (`forced` → `auto` → `no_think` → `none`) and raises
`agentscope.exception.StructuredOutputError` on failure.

**`ToolChoice`** (`agentscope.tool.ToolChoice`) is now a pydantic model, not a
string:

```python
class ToolChoice(BaseModel):
    mode: Literal["auto","none","required"] | str   # str = force one named tool
    tools: list[str] | None = None                  # optional whitelist / schema filter
```

---

## 4. Toolkit & tools (`agentscope.tool`)

`agentscope/tool/__init__.py` exports:
`AskUser`, `AskUserAnswer`, `AskUserMetadata`, `AskUserParams`, `ToolChoice`,
`Function`, `ToolBase`, `ParamsBase`, `ToolMiddlewareBase`, `MCPTool`,
`FunctionTool`, `ToolGroup`, `Toolkit`, `ToolChunk`, `ToolResponse`,
`RegisteredTool`, `BackendBase`, `LocalBackend`, `DirEntry`, `ExecResult`,
`ResetTools`, `Bash`, `PowerShell`, `Edit`, `Glob`, `Grep`, `Read`, `Write`,
`TaskUpdate`, `TaskGet`, `TaskList`, `TaskCreate`.

### 4.1 `ToolBase` — the tool protocol

```python
class ToolBase(ABC):
    name: str
    description: str
    input_schema: dict[str, Any]
    is_concurrency_safe: bool
    is_read_only: bool
    is_external_tool: bool = False
    is_state_injected: bool = False     # if True, agent injects kwarg `_agent_state`
    metadata_schema: dict | None = None
    is_mcp: bool = False
    mcp_name: str | None = None
    dangerous_files: list[str] = DEFAULT_DANGEROUS_FILES
    dangerous_directories: list[str] = DEFAULT_DANGEROUS_DIRECTORIES

    def __init__(self, middlewares: list[ToolMiddlewareBase] | None = None) -> None
    async def call(self, **kwargs) -> ToolChunk | AsyncGenerator[ToolChunk, None]  # override here
    async def __call__(self, *args, **kwargs) -> ...                                # do NOT override (middleware onion)
    @abstractmethod
    async def check_permissions(self, tool_input: dict, context: PermissionContext) -> PermissionDecision
    async def check_read_only(self, tool_input: dict) -> bool                       # defaults to is_read_only
    async def match_rule(self, rule_content: str | None, tool_input: dict) -> bool
    async def generate_suggestions(self, tool_input: dict) -> list[PermissionRule]
```

Tools are always invoked keyword-only; passing positional args raises `TypeError`.

### 4.2 `FunctionTool` — wrap a plain Python function

`FunctionTool` is the adapter (1.0's decorator is gone). Signature:

```python
class FunctionTool(ToolBase):
    def __init__(self,
                 func: Function,
                 name: str | None = None,                 # default: func.__name__
                 description: str | None = None,          # default: from docstring
                 input_schema: dict | type[BaseModel] | None = None,   # default: from type hints
                 is_concurrency_safe: bool = True,
                 is_read_only: bool = False,
                 is_state_injected: bool = False,
                 middlewares: list[ToolMiddlewareBase] | None = None,
                 permission: PermissionDecision | None = None) -> None: ...

    async def check_permissions(self, *_args, **_kwargs) -> PermissionDecision:
        # if `permission` was given -> return it; else ASK the user by default.
```

* **Name/description/schema derivation:** name ← `func.__name__`; description ←
  `_extract_func_description(func.__doc__)`, i.e. the docstring's leading
  description; schema ← `_extract_input_schema(func)`, built from the function's
  type annotations and docstring. Constraints (enums/ranges) are expressed with
  `typing.Literal` and `typing.Annotated[..., pydantic.Field(...)]`. You may pass
  `input_schema=` a JSON-schema `dict` or a pydantic `BaseModel` **subclass**
  (its `model_json_schema()` is used).
* **sync vs async:** `call()` awaits the function if it is a coroutine function,
  otherwise calls it synchronously, then normalises the result.
* **Return normalisation:** a returned `ToolChunk` passes through; a returned
  `AsyncGenerator`/`Generator` of `ToolChunk` (or of plain values) is streamed; a
  returned `str` becomes `ToolChunk(content=[TextBlock(text=result)])`; any other
  object is `json.dumps`-ed into a `TextBlock`. So a tool function that returns a
  plain `str` **still works**, but returning `ToolChunk` gives you full control.
* A `FunctionTool` with **no `permission`** defaults to `PermissionBehavior.ASK`.

### 4.3 Registering tools with `Toolkit`

```python
class Toolkit:
    def __init__(self,
                 tools: list[ToolBase] | None = None,
                 skills_or_loaders: Sequence[str | Skill | SkillLoaderBase] | None = None,
                 mcps: list[MCPClient] | None = None,
                 tool_groups: list[ToolGroup] | None = None,
                 meta_tool_response_template: str = DEFAULT_META_TOOL_RESPONSE_TEMPLATE,
                 skill_instruction_template: str = DEFAULT_SKILL_INSTRUCTION) -> None

    async def get_tool_schemas(self, groups: list[str] | None = None) -> list[dict]
    async def call_tool(self, tool_call: ToolCallBlock, state: AgentState) -> AsyncGenerator[ToolChunk | ToolResponse, None]
    async def add_tool(self, tool: ToolBase | list[ToolBase], group_name: str = "basic") -> None
    async def remove_tool(self, tool_name: str | list[str]) -> None
    async def get_tool(self, name: str) -> ToolBase | None
    async def check_tool_available(self, tool_name: str, activated_groups: list[str]) -> ToolBase
    async def get_skill_instructions(self, activated_groups: list[str] | None = None) -> str | None
    def clear(self) -> None
```

* There is **no `register_tool_function`**. Register either by passing
  `ToolBase` instances to the constructor (`Toolkit(tools=[...])`) or with
  `await toolkit.add_tool(FunctionTool(my_func, ...))`.
* Tools live in **tool groups** (`ToolGroup`); `"basic"` is the always-active
  default group. Extra groups are activated/deactivated by the agent through the
  built-in meta tool `reset_tools` (`ResetTools`).
* `Toolkit.call_tool(tool_call, state)` parses `tool_call.input` (with repair),
  validates against the tool's `input_schema`, injects `_agent_state` when
  `is_state_injected`, and yields `ToolChunk`s followed by a final `ToolResponse`.

`ToolGroup`:

```python
class ToolGroup:
    def __init__(self,
                 name: Literal["basic"] | str,
                 description: str | None = None,        # required unless name == "basic"
                 instructions: str | None = None,
                 tools: list[ToolBase] | None = None,
                 skills_or_loaders: Sequence[str | Skill | SkillLoaderBase] | None = None,
                 mcps: list[MCPClient] | None = None) -> None
```

### 4.4 `ToolResponse`, `ToolChunk`

```python
class ToolChunk(BaseModel):
    content: List[TextBlock | DataBlock]
    state: ToolResultState = ToolResultState.RUNNING
    is_last: bool = True
    metadata: dict = Field(default_factory=dict)
    id: str = Field(default_factory=_generate_id)

class ToolResponse(BaseModel):
    content: List[TextBlock | DataBlock] = Field(default_factory=list)
    state: Literal[ToolResultState.ERROR, ToolResultState.DENIED,
                   ToolResultState.INTERRUPTED, ToolResultState.SUCCESS] = ToolResultState.SUCCESS
    metadata: dict = Field(default_factory=dict)
    id: str = Field(default_factory=_generate_id)
    def append_chunk(self, chunk: ToolChunk) -> Self
```

**What a tool function should return:** an `AsyncGenerator[ToolChunk, None]` (or
`Generator`) to stream, or a `ToolChunk` for a single result; a plain `str` is
auto-wrapped by `FunctionTool`. `ToolChunk.state` defaults to `RUNNING` and is
promoted to `ERROR`/`DENIED`/`INTERRUPTED`/`SUCCESS` by the accumulation logic.

### 4.5 Minimal working example — two custom async tools

```python
import json
from agentscope.tool import Toolkit, FunctionTool, ToolChunk
from agentscope.message import TextBlock, ToolResultState
from agentscope.permission import PermissionDecision, PermissionBehavior

async def get_weather(city: str, units: str = "metric") -> ToolChunk:
    """Return the current weather for a city.

    Args:
        city: The city name, e.g. "Shanghai".
        units: "metric" or "imperial" (default "metric").
    """
    data = {"city": city, "units": units, "temp": 21}
    return ToolChunk(content=[TextBlock(text=json.dumps(data))])

async def search_docs(query: str, limit: int = 5):
    """Search the documentation and stream matches.

    Args:
        query: The search query.
        limit: Maximum number of results (1-20).
    """
    for i in range(limit):
        yield ToolChunk(content=[TextBlock(text=f"result {i}: {query}")])

toolkit = Toolkit(tools=[
    FunctionTool(get_weather, is_read_only=True,
                 permission=PermissionDecision(
                     behavior=PermissionBehavior.ALLOW,
                     message="Weather lookups are read-only.")),
    FunctionTool(search_docs, is_read_only=True,
                 permission=PermissionDecision(
                     behavior=PermissionBehavior.ALLOW,
                     message="Doc search is read-only.")),
])

# or dynamically:
# await toolkit.add_tool(FunctionTool(get_weather))
```

Note the schema is derived from the type hints (`city: str`, `limit: int = 5`)
and the docstring `Args:` section; constraints come from `Literal`/`Annotated`.

### 4.6 Builtin tools (all `ToolBase` subclasses)

| Class | `name` | `is_read_only` | `is_state_injected` | constructor |
|---|---|---|---|---|
| `Bash` | `"Bash"` | `False` | `False` | `Bash(dangerous_files=..., dangerous_directories=..., cwd=None, middlewares=None, backend=None)` |
| `PowerShell` | `"PowerShell"` | `False` | `False` | `PowerShell(cwd=None, middlewares=None, backend=None)` |
| `Read` | `"Read"` | `True` | `True` | `Read(max_line_characters=2000, model_input_types=None, middlewares=None, backend=None)` |
| `Write` | `"Write"` | `False` | `True` | `Write(dangerous_files=..., dangerous_directories=..., middlewares=None, backend=None)` |
| `Edit` | `"Edit"` | `False` | `True` | `Edit(dangerous_files=..., dangerous_directories=..., middlewares=None, backend=None)` |
| `Glob` | `"Glob"` | `True` | `False` | `Glob(backend=None, glob_helper_path=None, middlewares=None)` |
| `Grep` | `"Grep"` | `True` | `False` | `Grep(middlewares=None, backend=None)` |
| `AskUser` | `"AskUser"` | `True` | `False` | *(external tool, no args; `is_external_tool=True`)* |
| `SkillViewer` | `"Skill"` | `True` | `True` | `SkillViewer(get_skills_method, middlewares=None)` — created internally by `Toolkit` |
| `ResetTools` | `"reset_tools"` | `False` | `True` | `ResetTools(groups, response_template, middlewares=None)` — created internally by `Toolkit` |
| `TaskCreate`/`TaskGet`/`TaskList`/`TaskUpdate` | same as class name | `False` | `True` | no `__init__` (class-defaults) |

Input schemas (model-facing arguments):

* **Bash** — `command` (required), `description`, `timeout` (ms, ≤600000).
* **PowerShell** — analogous to Bash.
* **Read** — `file_path` (required), `offset` (1-based), `limit` (≤2000), `pages` (PDF range).
* **Write** — `file_path`, `content`.
* **Edit** — `file_path`, `old_string`, `new_string`, `replace_all`.
* **Glob** — `pattern` (required), `path`, `head_limit`, `offset`.
* **Grep** — `pattern` (required), `path`, `output_mode` (`content`/`files_with_matches`/`count`), `glob`, `type`, `-A`/`-B`/`-C`/`context`, `n`, `i`, …

`AskUser` is **external by nature** (`is_external_tool=True`): the agent yields a
`RequireExternalExecutionEvent` and the caller answers it. Its params are typed:
`AskUserParams(questions=[...])` with each question having 2–4 `_Option`s;
answers come back as `AskUserMetadata(answers=[AskUserAnswer(question, selected, other)])`.

`DEFAULT_DANGEROUS_FILES` (e.g. `.bashrc`, `.gitconfig`, `.ssh/config`, `.env` and
variants) and `DEFAULT_DANGEROUS_DIRECTORIES` (`[".git", ".vscode", ".idea", ".ssh"]`)
live in `tool/_constants.py`.

### 4.7 Tool middleware

```python
class ToolMiddlewareBase(ABC):
    @abstractmethod
    async def on_tool_call(self, tool, input_kwargs, next_handler) -> AsyncGenerator[ToolChunk, None]: ...
```
Attach via `FunctionTool(..., middlewares=[...])` or `ToolBase(middlewares=[...])`.
The **first** middleware is the outermost layer.

---

## 5. Agent (`agentscope.agent.Agent`)

`agentscope/agent/__init__.py` exports: `Agent`, `A2AAgent`, `RealtimeAgent`,
`TurnAggregator`, `TurnMetrics`, `ContextConfig`, `InjectionConfig`,
`ModelConfig`, `ReActConfig`.

### 5.1 Constructor

```python
class Agent:
    def __init__(self,
                 name: str,
                 system_prompt: str,
                 model: ChatModelBase,
                 toolkit: Toolkit | None = None,
                 middlewares: list[MiddlewareBase] | None = None,
                 state: AgentState | None = None,
                 offloader: Offloader | None = None,
                 model_config: ModelConfig | None = None,
                 context_config: ContextConfig | None = None,
                 react_config: ReActConfig | None = None,
                 injection_config: InjectionConfig | None = None) -> None
```

`state` defaults to a fresh `AgentState()`; the four configs default to their
`ModelConfig()`/`ContextConfig()`/`ReActConfig()`/`InjectionConfig()` defaults.
The agent builds `self._engine = PermissionEngine(self.state.permission_context)`.
`_validate_configs()` enforces `reserve_ratio < trigger_ratio` and (when runtime
state or the compression tool is on) `context_buffer_ratio < trigger_ratio`.

### 5.2 Config classes

```python
class ReActConfig(BaseModel):
    max_iters: int = 50
    structured_output_grace_iters: int = 5      # extra iters past max_iters for structured output
    stop_on_reject: bool = False                # stop replying when tool calls are rejected
    interruption_message: str = "I notice the interruption. How can I help you?"
    interruption_raise_cancelled_error: bool = False   # False = swallow CancelledError after cleanup

class ModelConfig(BaseModel):
    max_retries: int = 0                        # on top of the initial call, before fallback
    fallback_model: ChatModelBase | None = None

class ContextConfig(BaseModel):
    trigger_ratio: float = 0.8          # compress when tokens > ratio * context_size (max 0.9)
    reserve_ratio: float = 0.1          # tokens reserved during compression (< trigger_ratio)
    context_buffer_ratio: float = 0.2   # buffer ahead of trigger where compression is advised
    compression_prompt: str = "<system-hint>…</system-hint>"      # {current_time} placeholder
    summary_template: str = "<system-info>…</system-info>"
    summary_schema: dict = SummarySchema.model_json_schema()
    tool_result_limit: int = 50000      # max tool-result tokens before truncation
    compression_fallback_to_truncation: bool = True
    compression_tool_enabled: bool = False         # expose a `CompressContext` tool to the agent
    max_image_num: int = 5              # oldest images beyond this are offloaded/dropped

class InjectionConfig(BaseModel):
    inject_runtime_state: bool = True
    timezone: str = "UTC"
    time_format: str = "%Y-%m-%dT%H:%M:%S"
    time_interval: float = 0.5          # hours
    context_buffer_ratio: float | None = None   # DEPRECATED, use ContextConfig
    tool_retries_limit: int = 3         # consecutive identical failing calls -> error hint
    tool_retries_hint: str = "…{tool_name}…{count}…"
    template: str = "<system-reminder>…{runtime_state}…</system-reminder>"
    injection_source: str = '{"label": "System", "sublabel": "Runtime State"}'
    task_tool_names: list[str] = ["TaskCreate","TaskGet","TaskList","TaskUpdate"]
    extra_fields: dict[str, str] = {}
    emit_hint_event: bool = True
```

`SummarySchema` (used by the compressor) has fields `task_overview`,
`current_state`, `important_discoveries`, `next_steps`, `context_to_preserve`.

### 5.3 Public methods

```python
async def reply_stream(self,
                       inputs: Msg | list[Msg] | UserConfirmResultEvent | UserInterruptEvent | ExternalExecutionResultEvent | None = None,
                       structured_schema: Type[BaseModel] | None = None,
                       yield_final_msg: bool = False) -> AsyncGenerator[AgentEvent | Msg, None]

async def reply(self,
                inputs: Msg | list[Msg] | UserConfirmResultEvent | UserInterruptEvent | ExternalExecutionResultEvent | None = None,
                structured_schema: Type[BaseModel] | None = None) -> Msg

async def observe(self, msgs: Msg | list[Msg] | None = None) -> None
async def compress_context(self, context_config: ContextConfig | None = None, instructions: HintBlock | None = None) -> None
```

* `reply_stream` yields `AgentEvent`s; the final `Msg` is yielded **only** when
  `yield_final_msg=True`. `reply` consumes the stream and returns the final `Msg`
  (raising `RuntimeError` if none was produced).
* `inputs` variants:
  * `Msg`/`list[Msg]` — start a new reply;
  * `UserConfirmResultEvent` — resume a reply parked on user confirmation;
  * `ExternalExecutionResultEvent` — resume a reply parked on external execution;
  * `UserInterruptEvent` — abort a parked reply (closes pending tool calls, ends
    with `ReplyFinishedReason.INTERRUPTED`, no reasoning loop);
  * `None` — continue from current state.
* `yield_final_msg=True` is how you obtain **structured output**: the final `Msg`
  carries `.structured_output` (a dict) and `.finished_reason`.
* `observe(msgs)` writes messages into `state.context` without triggering a reply
  (replaces 1.0's `observe`).

**System prompt:** supplied as `system_prompt` in the constructor and stored in
`self._system_prompt`. At model-call time `_get_system_prompt()` concatenates the
base prompt, toolkit skill instructions, offloader instructions, then runs the
`on_system_prompt` middleware chain. You can mutate `agent._system_prompt`
(attribute) or use a `MiddlewareBase.on_system_prompt` hook to change it
dynamically. There is no separate `.set_system_prompt()`.

### 5.4 `AgentState` — where everything lives

`agentscope/state/_state.py`:

```python
class AgentState(BaseModel):
    session_id: str = Field(default_factory=_generate_id)
    summary: str | list[TextBlock | DataBlock] = ""     # compressed summary, prepended to context
    context: list[Msg] = Field(default_factory=list)    # the LIVE conversation (replaces 1.0 Memory)
    reply_context: ReplyContext = Field(default_factory=ReplyContext)       # reply_id, cur_iter, structured_schema, structured_output
    permission_context: PermissionContext = Field(default_factory=PermissionContext)
    tool_context: ToolContext = Field(default_factory=ToolContext)          # read_file_cache, activated_groups
    tasks_context: TaskContext = Field(default_factory=TaskContext)         # tasks: list[Task]
    middle_context: dict[str, Any] = Field(default_factory=dict)            # cross-reply scratch space for middlewares

    # convenience properties: reply_id, cur_iter (proxied to reply_context)
    def append_context(self, name, blocks) -> None
    def has_awaiting_tool_calls(self, name: str) -> bool
    def get_awaiting_tool_calls(self, name: str) -> list[ToolCallBlock]
    def get_unfinished_tool_calls(self, name: str) -> list[ToolCallBlock]
```

`ReplyContext` = `{reply_id, cur_iter, structured_schema, structured_output}`.
`ToolContext` also owns the `Read`-cache (`read_file_cache`, `max_cache_files=100`,
`max_cache_bytes=25000`) and `activated_groups`.

**Interrupt handling:** Ctrl+C / task cancellation is caught inside `_reply_impl`
as `asyncio.CancelledError`; the agent closes unfinished tool calls
(`_close_unfinished_tool_calls`), emits a `ReplyEndEvent(INTERRUPTED)` and a
fallback `AssistantMsg` with `react_config.interruption_message`. Set
`ReActConfig(interruption_raise_cancelled_error=True)` to re-raise.

---

## 6. Streaming events (`agentscope.event`)

`agentscope/event/_event.py`; `AgentEvent` is a `TypeAlias` union. Every event
derives from `EventBase` (`id`, `created_at`, `metadata`; `model_config =
ConfigDict(use_enum_values=True)`). `EventType` is a `StrEnum` with the same
names as the classes.

| Event | Carries | Notes |
|---|---|---|
| `ReplyStartEvent` | `session_id`, `reply_id`, `name`, `role="assistant"` | one per reply |
| `ReplyEndEvent` | `session_id`, `reply_id`, `finished_reason: ReplyFinishedReason`, `error: ErrorInfo\|None` | terminal |
| `ModelCallStartEvent` | `reply_id`, `model_name` | |
| `ModelCallEndEvent` | `reply_id`, `input_tokens`, `output_tokens`, `cache_input_tokens`, `cache_creation_input_tokens`, `finished_reason: FinishedReason` | usage |
| `TextBlockStartEvent` | `reply_id`, `block_id` | |
| `TextBlockDeltaEvent` | `reply_id`, `block_id`, `delta` | append text |
| `TextBlockEndEvent` | `reply_id`, `block_id`, `text: str\|None` | `text` only when final ≠ concat of deltas |
| `DataBlockStartEvent` | `reply_id`, `block_id`, `media_type`, `name` | |
| `DataBlockDeltaEvent` | `reply_id`, `block_id`, `media_type`, `data` **xor** `url` | exactly one of `data`/`url` (validator) |
| `DataBlockEndEvent` | `reply_id`, `block_id` | |
| `ThinkingBlockStartEvent` | `reply_id`, `block_id` | |
| `ThinkingBlockDeltaEvent` | `reply_id`, `block_id`, `delta` | |
| `ThinkingBlockEndEvent` | `reply_id`, `block_id` | |
| `HintBlockEvent` | `reply_id`, `block_id`, `source`, `hint` (one-shot, full content) | team msgs / background results |
| `ToolCallStartEvent` | `reply_id`, `tool_call_id`, `tool_call_name` | |
| `ToolCallDeltaEvent` | `reply_id`, `tool_call_id`, `delta` (JSON fragment) | |
| `ToolCallEndEvent` | `reply_id`, `tool_call_id` | |
| `ToolResultStartEvent` | `reply_id`, `tool_call_id`, `tool_call_name` | |
| `ToolResultTextDeltaEvent` | `reply_id`, `tool_call_id`, `delta` | |
| `ToolResultDataDeltaEvent` | `reply_id`, `tool_call_id`, `block_id`, `media_type`, `data` **xor** `url` | |
| `ToolResultEndEvent` | `reply_id`, `tool_call_id`, `state: ToolResultState`, `metadata` | |
| `RequireUserConfirmEvent` | `reply_id`, `tool_calls: List[ToolCallBlock]` | HITL: ask user |
| `RequireExternalExecutionEvent` | `reply_id`, `tool_calls: List[ToolCallBlock]` | HITL: external tool |
| `UserConfirmResultEvent` | `reply_id`, `confirm_results: list[ConfirmResult]` | **input** to resume |
| `UserInterruptEvent` | `reply_id` | **input** to abort a parked reply |
| `ExternalExecutionResultEvent` | `reply_id`, `execution_results: List[ToolResultBlock]` | **input** to resume |
| `CustomEvent` | `name: str`, `value: dict` | app-layer notifications (`"state_updated"`, `"team_updated"`, …) |
| `ExceedMaxItersEvent` | `reply_id`, `name` | **deprecated** — use `ReplyEndEvent.finished_reason == EXCEED_MAX_ITERS` |
| `ReplyEndReason` | — | **deprecated** alias of `agentscope.types.ReplyFinishedReason` |

```python
class ConfirmResult(BaseModel):
    confirmed: bool
    tool_call: ToolCallBlock
    rules: list[PermissionRule] | None = None   # only when confirmed; full rules to add
```

`ReplyFinishedReason` (`agentscope.types._reply`):
`COMPLETED`, `INTERRUPTED`, `EXCEED_MAX_ITERS`, `ERROR`.
`ErrorInfo` = `{type: ErrorType, message: str}`.

### 6.1 Worked mini-example — consume `reply_stream` into UI text/tool records

```python
from agentscope.message import UserMsg, AssistantMsg
from agentscope.event import (
    ReplyStartEvent, ReplyEndEvent, TextBlockDeltaEvent,
    ThinkingBlockDeltaEvent, ToolCallEndEvent, ToolResultEndEvent,
    RequireUserConfirmEvent, ModelCallEndEvent,
)

async def render_reply(agent, user_text: str):
    accumulated = None
    tool_calls = []          # [{id, name, input_json}]
    tool_results = []        # [{id, name, state, text}]

    async for event in agent.reply_stream(UserMsg("user", user_text)):
        if isinstance(event, ReplyStartEvent):
            accumulated = AssistantMsg(event.name, [], id=event.reply_id)
        if accumulated is not None:
            accumulated.append_event(event)      # keep a full Msg

        if isinstance(event, TextBlockDeltaEvent):
            print(event.delta, end="")           # stream UI text
        elif isinstance(event, ThinkingBlockDeltaEvent):
            print(f"[think] {event.delta}", end="")
        elif isinstance(event, ToolCallEndEvent):
            block = accumulated._find_block("tool_call", event.tool_call_id)
            tool_calls.append({"id": event.tool_call_id,
                               "name": block.name, "input": block.input})
        elif isinstance(event, ToolResultEndEvent):
            block = accumulated._find_block("tool_result", event.tool_call_id)
            tool_results.append({"id": event.tool_call_id, "name": block.name,
                                 "state": event.state, "output": block.output})
        elif isinstance(event, RequireUserConfirmEvent):
            return event                          # park: hand back to caller (see §7)
        elif isinstance(event, ReplyEndEvent):
            print(f"\n[finished: {event.finished_reason}]")

    return accumulated
```

For structured output, call `reply_stream(..., structured_schema=MyModel,
yield_final_msg=True)` and read `msg.structured_output` on the `Msg`.

---

## 7. HITL / permission (`agentscope.permission`)

`agentscope/permission/__init__.py` exports: `PermissionContext`,
`AdditionalWorkingDirectory`, `PermissionDecision`, `PermissionEngine`,
`PermissionRule`, `PermissionMode`, `PermissionBehavior`.

### 7.1 Types

```python
class PermissionMode(Enum):
    DEFAULT = "default"           # ask unless an allow rule / read-only / tool ALLOW
    ACCEPT_EDITS = "accept_edits" # auto-allow edits inside working directories
    EXPLORE = "explore"           # read-only; modifications denied
    BYPASS = "bypass"             # skip safety ASKs; deny/ask rules still apply
    DONT_ASK = "dont_ask"         # convert every ASK to DENY (unattended)

class PermissionBehavior(Enum):
    ALLOW = "allow"; DENY = "deny"; ASK = "ask"; PASSTHROUGH = "passthrough"

class PermissionRule(BaseModel):
    tool_name: str
    rule_content: str | None       # Bash=substring, Read/Write/Edit=glob, others=generic
    behavior: PermissionBehavior
    source: str                    # "userSettings", "projectSettings", …  (required)

@dataclass
class PermissionDecision:
    behavior: PermissionBehavior
    message: str
    decision_reason: str | None = None
    updated_input: dict | None = None
    suggested_rules: list[PermissionRule] | None = None
    bypass_immune: bool = False    # ASK that no allow rule may override (safety)

class PermissionContext(BaseModel):
    mode: PermissionMode = PermissionMode.DEFAULT
    working_directories: dict[str, AdditionalWorkingDirectory] = {}
    allow_rules: dict[str, list[PermissionRule]] = {}   # keyed by tool name
    deny_rules:  dict[str, list[PermissionRule]] = {}
    ask_rules:   dict[str, list[PermissionRule]] = {}

class PermissionEngine:
    def __init__(self, context: PermissionContext) -> None
    def add_rule(self, rule: PermissionRule) -> None
    async def check_permission(self, tool: ToolBase, tool_input: dict) -> PermissionDecision
```

### 7.2 Where the mode/rules live

On the agent: **`AgentState.permission_context`** (`agentscope.state.AgentState`,
field `permission_context: PermissionContext`). The `Agent` constructs
`PermissionEngine(self.state.permission_context)` in its `__init__`, so mutating
`agent.state.permission_context.mode` / `.allow_rules` / … takes effect for the
next tool call. `Agent._check_permission_impl` calls
`self._engine.check_permission(tool, tool_input)`.

**Per-tool-call vs per-agent:** the *rules / mode / working directories* are
**per-agent** (one `PermissionContext` per `AgentState`). The *decision* is
**per tool call** — the engine is invoked for each `ToolCallBlock` with that
call's parsed `tool_input`, so different invocations of the same tool (different
commands / paths) get different decisions. `PermissionEngine._rule_matches`
delegates to `tool.match_rule(rule_content, tool_input)`, so per-call matching
lives on the tool (`Bash` prefix/substring, `Read`/`Write`/`Edit` glob).

### 7.3 How a tool declares it needs confirmation

Two ways:

1. **`FunctionTool(..., permission=PermissionDecision(behavior=ASK, message="…"))`**
   — a fixed decision for every invocation (the default when `permission` is
   `None` is `ASK`).
2. **Override `ToolBase.check_permissions(tool_input, context)`** on a custom
   `ToolBase` subclass and return the decision based on the input
   (e.g. `Bash` returns a bypass-immune ASK for `rm -rf /`).

Mode/rule evaluation order (DEFAULT, `_check_default`): deny rules → ask rules →
read-only fast path → tool's `check_permissions` → allow rules → fallback ASK
(with `suggested_rules`).

### 7.4 The exact end-to-end confirmation loop

1. The agent evaluates permission for a tool call inside `_execute_tool_call`.
   On `ASK` it sets the call's state to `ASKING`, stores
   `tool_call.suggested_rules = decision.suggested_rules`, and **yields**
   `RequireUserConfirmEvent(reply_id=..., tool_calls=[tool_call])`.
2. `reply_stream` returns control to your loop (the agent is now *parked*).
3. Your code builds a `UserConfirmResultEvent` and feeds it back:

```python
from agentscope.event import ConfirmResult, UserConfirmResultEvent

pending = ...  # the RequireUserConfirmEvent you received
results = [
    ConfirmResult(
        confirmed=True,
        tool_call=tc,                       # the same ToolCallBlock from pending.tool_calls
        rules=tc.suggested_rules or None,   # pass suggestions to remember "always allow"
    )
    for tc in pending.tool_calls
]
confirm = UserConfirmResultEvent(reply_id=pending.reply_id, confirm_results=results)

async for event in agent.reply_stream(confirm):
    ...   # the agent continues: ALLOWED calls execute, denied calls become DENIED results
```

Denied calls produce a `ToolResultBlock` with `state=ToolResultState.DENIED` and
the message `The execution of tool "X" is denied by user!`. Confirmed calls get
their state flipped to `ALLOWED` with `tool_call.name`/`.input` copied from your
`ConfirmResult` (so the caller may edit the call), and `rules` are added to the
engine via `self._engine.add_rule(rule)`.

If instead a tool is `is_external_tool=True` (e.g. `AskUser`), the agent yields
`RequireExternalExecutionEvent`; you run it and resume with
`ExternalExecutionResultEvent(reply_id=..., execution_results=[ToolResultBlock(...)])`.

To abort a parked reply, feed `UserInterruptEvent(reply_id=pending.reply_id)`: the
agent closes every pending tool call with an INTERRUPTED result and ends with
`ReplyFinishedReason.INTERRUPTED`.

### 7.5 Console reference (`agentscope/console/_console.py`)

`_run_reply()` wraps `agent.reply_stream(inputs)` in a task, renders every event,
and captures the **last** `RequireUserConfirmEvent` it sees as `pending`. It
returns that pending event; the outer `launch_console` loop then calls
`_confirm(pending)`:

```python
async def _confirm(pending: RequireUserConfirmEvent) -> UserConfirmResultEvent:
    results = []
    for tool_call in pending.tool_calls:
        prompt = f"Allow '{tool_call.name}'? [y]es / [N]o"
        if tool_call.suggested_rules:
            prompt += " / [a]lways"
        answer = (await asyncio.to_thread(input, f"{prompt} ")).strip().lower()
        always = bool(tool_call.suggested_rules) and answer in ("a", "always")
        results.append(ConfirmResult(
            confirmed=always or answer in ("y", "yes"),
            tool_call=tool_call,
            rules=tool_call.suggested_rules if always else None,
        ))
    return UserConfirmResultEvent(reply_id=pending.reply_id, confirm_results=results)
```

and feeds the result back:

```python
while True:
    pending = await _run_reply(agent, renderer, inputs)
    if pending is None:
        break
    try:
        inputs = await _confirm(pending)
    except (EOFError, KeyboardInterrupt):
        inputs = UserInterruptEvent(reply_id=pending.reply_id)   # abort the parked reply
```

---

## 8. Middleware (`agentscope.middleware`)

`agentscope/middleware/__init__.py` exports `MiddlewareBase` plus optional
built-ins (`middleware/_budget.py`, `_model_router.py`, `_rag.py`,
`_longterm_memory/`, `_tracing/`, `_tts_middleware.py`).

```python
class MiddlewareBase:
    def is_implemented(self, hook_name: str) -> bool
```

Hooks (all optional; only overridden ones are registered — `Agent.__init__`
filters each list by `is_implemented(hook)`):

| Hook | Signature | Pattern |
|---|---|---|
| `on_reply` | `async (self, agent, input_kwargs, next_handler) -> AsyncGenerator[AgentEvent｜Msg, None]` | onion |
| `on_reasoning` | `async (self, agent, input_kwargs, next_handler) -> AsyncGenerator` | onion |
| `on_check_permission` | `async (self, agent, input_kwargs, next_handler) -> PermissionDecision` | onion (returns a value) |
| `on_acting` | `async (self, agent, input_kwargs, next_handler) -> AsyncGenerator[ToolChunk｜ToolResponse, None]` | onion (wraps only `toolkit.call_tool`) |
| `on_model_call` | `async (self, agent, input_kwargs, next_handler) -> ChatResponse ｜ AsyncGenerator[ChatResponse, None]` | onion |
| `on_compress_context` | `async (self, agent, input_kwargs, next_handler) -> None` | onion |
| `on_system_prompt` | `async (self, agent, current_prompt: str) -> str` | **transformer** (sequential) |

`input_kwargs` contents: `on_reply` → `{"inputs"}`; `on_reasoning` →
`{"tool_choice"}`; `on_acting` → `{"tool_call"}`; `on_check_permission` →
`{"tool_call", "tool", "tool_input"}`; `on_model_call` → `{"messages", "tools",
"tool_choice", "current_model"}`; `on_compress_context` → `{"context_config",
"instructions"}`.

Register by passing a list to the `Agent(..., middlewares=[...])` constructor
(there is no `add_middleware` method). Optional `list_tools()` and
`get_middleware_key()` hooks exist (the latter for persisting middleware state in
`AgentState.middle_context`).

### 8.1 Example: inject a dynamic system prompt

```python
from agentscope.middleware import MiddlewareBase
from datetime import datetime

class DynamicPromptMiddleware(MiddlewareBase):
    async def on_system_prompt(self, agent, current_prompt: str) -> str:
        return current_prompt + f"\n\nCurrent time: {datetime.now().isoformat()}"
```

### 8.2 Example: force confirmation for certain tools

```python
from agentscope.middleware import MiddlewareBase
from agentscope.permission import PermissionDecision, PermissionBehavior

class ForceConfirmMiddleware(MiddlewareBase):
    FORCE = {"Write", "Bash", "Edit"}

    async def on_check_permission(self, agent, input_kwargs, next_handler):
        tool_input = input_kwargs["tool_input"]
        tool = input_kwargs["tool"]
        if tool.name in self.FORCE:
            return PermissionDecision(
                behavior=PermissionBehavior.ASK,
                message=f"{tool.name} requires confirmation.",
            )
        return await next_handler(**input_kwargs)   # delegate to the engine
```

---

## 9. Multi-agent / teams (`agentscope.pipeline`, `agentscope.app._tool`)

`agentscope/pipeline/__init__.py` exports: `PipelineProtocol`,
`GoalPipeline`, `TeamMember`, `TeamPipeline`.

`PipelineProtocol` is a `Protocol` with only `reply_stream(inputs) ->
AsyncGenerator[AgentEvent | Msg, None]`, so `Agent`, `TeamPipeline` and
`GoalPipeline` are interchangeable to callers (the console accepts
`Agent | PipelineProtocol`).

### 9.1 `TeamPipeline` — leader + members via a tool call

```python
class TeamMember(BaseModel):     # ConfigDict(arbitrary_types_allowed=True)
    agent: Agent
    description: str             # what the member is good at; shown to the leader

class TeamPipeline:
    def __init__(self, leader: Agent, members: list[TeamMember], reset_members: bool = True) -> None
    async def reply_stream(self, inputs: Msg | list[Msg] | UserConfirmResultEvent
                           | UserInterruptEvent | ExternalExecutionResultEvent | None = None,
                           ) -> AsyncGenerator[AgentEvent, None]
```

* Registers an **external** tool `TeamAssign` (`_TeamAssign`, `is_external_tool=True`)
  on the leader's toolkit. Its schema is `{member: enum(names), prompt: str}`.
* When the leader calls `TeamAssign`, the pipeline runs that member's
  `reply_stream(UserMsg("user", prompt), yield_final_msg=True)` in the member's
  own context and turns its final reply into the tool result (state mapped from
  `finished_reason` via `_RESULT_STATES`). The leader sees only the member's final
  reply, not intermediate steps.
* Assignments to different members run **concurrently** (`asyncio.gather`), while
  assignments to the **same** member run sequentially in call order.
* HITL is **per participant**: a member that needs confirmation parks like any
  agent; its `RequireUserConfirmEvent` is streamed out unchanged, and the resume
  event is routed to the parked participant by `reply_id`
  (`_parked_member(inputs.reply_id)`). `UserInterruptEvent` aborts every parked
  participant.
* With `reset_members=True` (default) each member's `state.context`/`summary` is
  cleared after the leader's reply ends.

### 9.2 `GoalPipeline` — executor/verifier loop

```python
class GoalPipeline:
    def __init__(self, executor: Agent, verifier: Agent,
                 verifier_reset_context: bool = True,
                 max_iters: int = 10, max_retries: int = 3) -> None
    async def reply_stream(self, inputs) -> AsyncGenerator
```

* The executor runs with `structured_schema=_ExecutionReport` (`{report: str}`),
  the verifier with `structured_schema=_VerificationResult`
  (`{result: "pass"|"fail"|"impossible", message: str}`).
* Loop: executor produces a report → verifier judges → `pass`/`impossible` ends
  the loop; `fail` feeds the verifier's `message` back to the executor as a
  system-reminder user message and re-runs, up to `max_iters`.
* HITL events from either agent park the whole pipeline and are routed back by
  `reply_id` (executor's reply_id vs verifier's). `verifier_reset_context` clears
  the verifier's conversation between rounds.

### 9.3 Team tools in `agentscope.app._tool` (app-level, high support)

`agentscope/app/_tool/__init__.py` exports: `AgentCreate`, `AgentInvite`,
`DEFAULT_SUB_AGENT_TEMPLATE`, `SubmitHandover`, `SubmitVerdict`, `TeamCreate`,
`TeamDelete`, `TeamSay`. These are wired into team-participating agents by the
**app** layer (`get_toolkit`), not by the core library:

* `TeamCreate` — create a team led by the current session, return its id.
* `AgentCreate` — spawn a new worker (own role/prompt/permission mode) from a
  `SubAgentTemplate`; the member starts working immediately.
* `AgentInvite` — **borrow** a pre-existing user-owned agent into the team by
  minting a team-scoped `SessionRecord` over the existing `AgentRecord`
  (keeps its system prompt/config; cross-owner borrow gets the caller's
  workspace/model, not the owner's).
* `TeamSay` — send a message to one member or broadcast; workers use it to report
  back to the leader (worker route) / the leader uses it for coordination.
* `TeamDelete` — dissolve the team (delegates to `SessionService`).

All team tools extend `_TeamToolBase` and allow themselves unconditionally
(`check_permissions` → ALLOW); visibility is gated by the session's team role,
not by per-tool permission.

**For a custom "roundtable" system:** 2.0 has first-class team support, but there
are **two distinct layers**: the *library-level* `TeamPipeline` (a leader
assigning tasks to members via an external `TeamAssign` tool, results flowing back
as tool results) and the *app-level* tool suite (`TeamCreate`/`AgentCreate`/
`AgentInvite`/`TeamSay`) which additionally requires the app's `StorageBase`,
`MessageBus` and workspace manager. `TeamPipeline` is members-never-talk-to-each-
other (hub-and-spoke); a peer roundtable where members message each other maps to
the app-level `TeamSay`/`HintBlock` model rather than to `TeamPipeline`. There is
**no `msghub`/`chatroom` module** in 2.0.

---

## 10. Session/state persistence & memory

* **Memory replacement:** 1.0's `InMemoryMemory` is gone. The conversation is
  `AgentState.context: list[Msg]`; the compressed summary is `AgentState.summary`.
  Tool cache, activated groups, tasks, permission rules and middleware scratch all
  live on the same `AgentState`.
* **No `agentscope.session` package.** There is no JSON/Redis session helper in
  the core library (`agentscope/session/` does not exist). Session management
  (status, cancel, delete, Redis/JSON storage) lives in the **app** layer:
  `agentscope/app/_service/_session.py` (`SessionService`) and
  `agentscope/app/storage/` (`StorageBase` with `state: AgentState` on the
  session record; `_redis_storage.py`, SQL via `_sql/_storage.py`). This is only
  usable if you run the full `agentscope.app` server.
* **Save / restore state:** `AgentState` is a pydantic `BaseModel`, so serialise
  with `agent.state.model_dump_json()` / `AgentState.model_validate_json(raw)`
  (the app stores `session.state` this way). There is no core `state_dict()` /
  `load_state_dict()`; you persist the whole state object. `Agent(state=...)`
  accepts a pre-built `AgentState`. `ReplyContext._serialize_structured_schema`
  turns a pydantic class into its JSON-schema dict, and
  `AgentState._migrate_legacy_reply_fields` migrates the older top-level
  `reply_id`/`cur_iter` format.
* The `launch_console` entry explicitly has "no session management, no
  persistence: the conversation lives in `agent.state` and ends with the process."
* Long-term memory, if needed, is a **middleware**:
  `agentscope.middleware._longterm_memory` provides mem0 / ReMe / agentic-memory
  middlewares.

---

## 11. Formatter (`agentscope.formatter`)

`agentscope/formatter/__init__.py` exports `FormatterBase` and, per provider, a
`*ChatFormatter` (single-agent / tool-call style) and a `*MultiAgentFormatter`
(renders other agents' messages as text, for group chat):

`DashScopeChatFormatter`/`DashScopeMultiAgentFormatter`,
`AnthropicChatFormatter`/`AnthropicMultiAgentFormatter`,
`OpenAIChatFormatter`/`OpenAIMultiAgentFormatter`,
`GeminiChatFormatter`/`GeminiMultiAgentFormatter`,
`OllamaChatFormatter`/`OllamaMultiAgentFormatter`,
`DeepSeekChatFormatter`/`DeepSeekMultiAgentFormatter`,
`OpenAIResponseFormatter`/`OpenAIResponseMultiAgentFormatter`,
`MiniMaxChatFormatter`/`MiniMaxMultiAgentFormatter`,
`MoonshotChatFormatter`/`MoonshotMultiAgentFormatter`,
`XAIChatFormatter`/`XAIMultiAgentFormatter`,
`VolcengineChatFormatter`/`VolcengineMultiAgentFormatter`.

```python
class FormatterBase(BaseModel):
    input_types: list[str] = ["text/plain"]     # aligned with the model card's input_types
    @property
    def supported_input_media_types(self) -> list[str]
    @abstractmethod
    async def format(self, *args, **kwargs) -> list[dict[str, Any]]
    # helpers: assert_list_of_msgs, convert_tool_result_to_string, _group_messages
```

**Attachment:** a formatter attaches to the **model**, not the agent. Every chat
model's `__init__` takes `formatter: FormatterBase | None` and defaults to its own
(e.g. `OpenAIChatModel` → `OpenAIChatFormatter()`), storing it as `self.formatter`
and calling `await self.formatter.format(messages)` inside `_call_api`. The
`Agent` never sees the formatter. Choose the `MultiAgentFormatter` variant when
several agents share one model and their messages must be flattened to text.

---

## 12. Gotchas — 1.0 → 2.0 removals & easy mistakes

**Removed / renamed (does not exist in 2.0):**

| 1.0 | 2.0 replacement |
|---|---|
| `InMemoryMemory` | `AgentState.context: list[Msg]` + `AgentState.summary` |
| `OpenAITokenCounter` | `ChatModelBase.count_tokens(messages, tools)` (byte/4 estimate) |
| `stream_printing_messages` | iterate `Agent.reply_stream()` |
| `ReActAgent` | `Agent` (single class) + `ReActConfig` |
| `ToolUseBlock` | `ToolCallBlock` (`agentscope.message`) |
| `Toolkit.register_tool_function` | `FunctionTool(func, ...)` + `Toolkit.add_tool(tool)` / `Toolkit(tools=[...])` |
| decorator `@tool` / `register_tool` | `FunctionTool` adapter |
| `Msg("user", "text")` positional | `UserMsg(name, content)`; `Msg` is pydantic → **keyword-only** |
| `Msg.content: str` | `Msg.content: list[ContentBlock]`; use `get_text_content()` |
| per-agent string `tool_choice="auto"` | `ToolChoice(mode="auto")` (pydantic model) |
| `msghub` / `chatroom` | no direct equivalent; `TeamPipeline` (hub-and-spoke) or app-level `TeamSay` |
| `agentscope.session` | not in core; app-layer `SessionService` + storage; or dump `AgentState` yourself |

**Easy to get wrong:**

* **`UserMsg`/`AssistantMsg`/`SystemMsg` are functions, not classes** — you
  cannot `isinstance(msg, UserMsg)`; test `msg.role`.
* **`Msg` is keyword-only.** `Msg("a", "b", "user")` raises; use `Msg(name=...,
  role=..., content=[...])`.
* **User messages reject non-text/data blocks** (`_assert_user_content_blocks`);
  system messages reject everything but `TextBlock`.
* **`FunctionTool` defaults to ASK.** A custom tool with no `permission=` will
  prompt the user every time; pass a `PermissionDecision(ALLOW, ...)` or set
  `is_read_only=True` (which the read-only fast path auto-allows in every mode).
* **Tools are keyword-only.** `ToolBase.__call__` raises `TypeError` on positional
  args.
* **`is_state_injected=True` tools receive `_agent_state`** as a keyword arg — the
  function must accept it.
* **Return type:** prefer `ToolChunk` / async generator of `ToolChunk` from a tool
  function; a bare `str` is auto-wrapped, but a bare `dict` is JSON-stringified.
* **The formatter is on the model**, not the agent — `Agent(...)` has no
  `formatter` argument.
* **`reply_stream` does not yield the final `Msg` by default** — pass
  `yield_final_msg=True` (required to read `structured_output`).
* **HITL is a resume protocol:** you must feed back a `UserConfirmResultEvent`
  whose `reply_id` matches the parked `RequireUserConfirmEvent`; a mismatched or
  missing event raises `ValueError` in `_check_incoming_event`.
* **`UserInterruptEvent` only aborts a *parked* reply**; to interrupt an actively
  generating reply, cancel the task (the agent handles `CancelledError`).
* **Permission rules are per-agent, decisions per tool call**; `PermissionRule`
  requires `source` (e.g. `"userSettings"`), and `rule_content=None` matches every
  invocation of that tool name.
* **`ExceedMaxItersEvent` / `ReplyEndReason` are deprecated** — check
  `ReplyEndEvent.finished_reason` (`ReplyFinishedReason.EXCEED_MAX_ITERS`) instead.
* **`TextBlockEndEvent.text`** is only set when the final text differs from the
  concatenation of deltas; normally you accumulate deltas.
* `DataBlockDeltaEvent`/`ToolResultDataDeltaEvent` require **exactly one** of
  `data` or `url` (pydantic validator).
* **Agent calls are all async** — `await agent.reply(...)`, `async for ... in
  agent.reply_stream(...)`, `await agent.observe(...)`.
* **Tool groups:** extra `ToolGroup`s beyond `"basic"` are inert until the agent
  activates them by calling the built-in `reset_tools` (`ResetTools`) meta tool.
