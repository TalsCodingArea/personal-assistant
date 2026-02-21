from langchain_core.prompts import ChatPromptTemplate

INTENT_PROMPT = ChatPromptTemplate.from_messages([
    ("system", "Classify the user's message into ONE label: 'finance', 'movies', or 'general'. Return only the label."),
    ("human", "{text}")
])

async def classify_intent(llm, text: str) -> str:
    resp = await llm.ainvoke(INTENT_PROMPT.format_messages(text=text))
    label = (resp.content or "").strip().lower()
    if label not in {"finance", "movies", "general"}:
        return "general"
    return label