"""A fake model that makes a fixed sequence of tool calls, then returns the final result."""

from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel


def scripted_model(steps_by_agent: dict[str, list[tuple[str, dict]]]) -> FunctionModel:
    def next_call(messages, info: AgentInfo) -> tuple[str, dict]:
        first = messages[0].parts[-1].content
        agent = next(a for a in steps_by_agent if f'(id: {a})' in first)
        steps = steps_by_agent[agent]
        done = sum(isinstance(m, ModelResponse) for m in messages)
        if done < len(steps):
            return steps[done]
        return info.output_tools[0].name, {'public_update': f'{agent} finished step {done}', 'memory': f'{agent} memory'}

    def fn(messages, info):
        name, args = next_call(messages, info)
        return ModelResponse(parts=[ToolCallPart(name, args)])

    async def stream_fn(messages, info):
        name, args = next_call(messages, info)
        import json
        yield {0: DeltaToolCall(name=name, json_args=json.dumps(args))}

    return FunctionModel(fn, stream_function=stream_fn)
