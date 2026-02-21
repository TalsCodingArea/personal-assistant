from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.runnables.history import RunnableWithMessageHistory
from langchain_classic.agents import AgentExecutor, create_tool_calling_agent

from agent.system_prompt import SYSTEM_PROMPT
from agent.contexts.base_context import BASE_CONTEXT
from agent.contexts.financial_context import FINANCIAL_CONTEXT
from agent.contexts.movie_context import MOVIE_CONTEXT

from tools.registry import get_tools
from agent.memory import MemoryStore


def build_prompt(context_block: str):
    return ChatPromptTemplate.from_messages([
        ("system", SYSTEM_PROMPT.strip() + "\n\n" + context_block.strip()),
        MessagesPlaceholder(variable_name="history"),
        ("human", "{input}"),
        MessagesPlaceholder(variable_name="agent_scratchpad"),
    ])


def build_agent(llm, memory_store: MemoryStore, context_label: str):
    tools = get_tools()

    if context_label == "finance":
        prompt = build_prompt(FINANCIAL_CONTEXT)
    elif context_label == "movies":
        prompt = build_prompt(MOVIE_CONTEXT)
    else:
        prompt = build_prompt(BASE_CONTEXT)

    agent = create_tool_calling_agent(llm, tools, prompt)
    executor = AgentExecutor(agent=agent, tools=tools, verbose=True)

    return RunnableWithMessageHistory(
        executor,
        lambda session_id: memory_store.get_history(session_id),
        input_messages_key="input",
        history_messages_key="history",
    )