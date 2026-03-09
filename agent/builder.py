from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.runnables.history import RunnableWithMessageHistory
from langchain_classic.agents import AgentExecutor, create_tool_calling_agent

from agent.system_prompt import SYSTEM_PROMPT
from tools.registry import get_tools
from agent.memory import MemoryStore


def _escape_prompt_braces(text: str) -> str:
    # ChatPromptTemplate uses {} for variables, so escape literal braces in static text.
    return text.replace("{", "{{").replace("}", "}}")


def build_prompt():
    system_text = _escape_prompt_braces(SYSTEM_PROMPT.strip())
    return ChatPromptTemplate.from_messages([
        ("system", system_text),
        MessagesPlaceholder(variable_name="history"),
        ("human", "{input}"),
        MessagesPlaceholder(variable_name="agent_scratchpad"),
    ])


def build_agent(llm, memory_store: MemoryStore, extra_tools=None):
    tools = get_tools()
    if extra_tools:
        tools = tools + list(extra_tools)

    prompt = build_prompt()
    agent = create_tool_calling_agent(llm, tools, prompt)
    executor = AgentExecutor(agent=agent, tools=tools, verbose=True)

    return RunnableWithMessageHistory(
        executor,
        lambda session_id: memory_store.get_history(session_id),
        input_messages_key="input",
        history_messages_key="history",
    )
