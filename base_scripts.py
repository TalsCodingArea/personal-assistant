from openai import OpenAI
import requests
from notion_client import Client



def create_notion_page(notion_client: Client, database_id: str, props: dict):
    """Create a new page in a Notion database with the given properties.
     Args:
        notion_client (Client): An instance of the Notion client.
        database_id (str): The ID of the Notion database where the page will be created.
        props (dict): A dictionary of properties to set on the new page.
    """

    for content, prop in props.items():
        if prop["type"] == "title":
            props[content] = {
                "title": [{"type": "text", "text": {"content": prop["content"]}}]
            }
        if prop["type"] == "text":
            props[content] = {
                "rich_text": [{"type": "text", "text": {"content": prop["content"]}}]
            }
        if prop["type"] == "select":
            props[content] = {
                "select": {"name": prop["content"]}
            }
        if prop["type"] == "multi_select":
            props[content] = {
                "multi_select": [{"name": tag} for tag in prop["content"]]
            }
        if prop["type"] == "number":
            props[content] = {
                "number": prop["content"]
            }
        if prop["type"] == "checkbox":
            props[content] = {
                "checkbox": prop["content"]
            }
        if prop["type"] == "date":
            props[content] = {
                "date": {"start": prop["content"]}
            }
        if prop["type"] == "file":
            props[content] = {
                "files": [
                    {
                        "name": f"{content}.pdf",
                        "type": "external",
                        "external": {"url": prop["content"]}
                    }
                ]
            }
    page = notion_client.pages.create(
        parent={"database_id": database_id},
        properties=props
    )

def get_notion_pages(notion_client: Client, database_id: str, filter: dict = None, sorts: list = []):
    """Retrieve pages from a Notion database with optional filtering.
    Args:
        notion_client (Client): An instance of the Notion client.
        database_id (str): The ID of the Notion database to query.
        filter (dict, optional): A filter object to apply to the query. Defaults to None.
        sorts (list, optional): A list of sort objects to apply to the query. Defaults to None.
    Returns:
        list: A list of pages matching the query.
    """
    query = {
        "filter": filter,
        "sorts": sorts
    }
    response = notion_client.databases.query(database_id, **{k: v for k, v in query.items() if v})
    return response.get("results", [])

def ask_openai(prompt: str, model: str = "gpt-4o", temperature: float = 0.7) -> str:
    """Send a prompt to the OpenAI API and return the response.
    Args:
        prompt (str): The prompt to send to the OpenAI API.
        model (str, optional): The model to use for the completion. Defaults to "gpt-4o".
        temperature (float, optional): The temperature for the completion. Defaults to 0.7.
    Returns:
        str: The response from the OpenAI API.
    """
    client = OpenAI()
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": prompt}
        ],
        temperature=temperature
    )
    return response.choices[0].message.content