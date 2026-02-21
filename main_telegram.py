import asyncio
from agent.llm import get_llm
from agent.memory import MemoryStore
from agent.builder import build_agent
from router.intent_router import classify_intent

async def main():
    llm = get_llm()
    memory = MemoryStore()

    session_id = "local-test"

    while True:
        text = input("You: ").strip()
        if text.lower() in {"exit", "quit"}:
            break

        intent = await classify_intent(llm, text)
        agent = build_agent(llm, memory, intent)
        out = await agent.ainvoke({"input": text}, config={"configurable": {"session_id": session_id}})
        print("Assistant:", out.get("output", ""))

if __name__ == "__main__":
    asyncio.run(main())