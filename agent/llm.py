import os
from dotenv import load_dotenv

from langchain_openai import ChatOpenAI

load_dotenv()

def get_llm(model: str | None = None):
    """
    Central place to configure the LLM.
    Switch models by passing model=... or via env var ASSISTANT_LLM_MODEL.
    """
    chosen_model = model or os.getenv("ASSISTANT_LLM_MODEL", "gpt-4o-mini")
    temperature = float(os.getenv("ASSISTANT_LLM_TEMPERATURE", 0.7))
    return ChatOpenAI(model=chosen_model, temperature=temperature)