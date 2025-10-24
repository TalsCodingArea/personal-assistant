from xmlrpc import client
from base_scripts import *
import os
from datetime import datetime

notion_client = Client(auth=os.environ["NOTION_API_KEY"])
openai_client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
omdb_api_key = os.environ["OMDB_API_KEY"]

def add_movie(movie_name: str, year: int = None):
    """Get movie information from OMDB and add it to the Notion database.
    Args:
        movie_name (str): The name of the movie to search for.
        year (int, optional): The release year of the movie. Defaults to None.

    Returns:
        dict: The movie information retrieved from OMDB.
    """
    if year:
        response = requests.get(f"http://www.omdbapi.com/?apikey={omdb_api_key}&s={movie_name}&y={year}")
    else:
        response = requests.get(f"http://www.omdbapi.com/?apikey={omdb_api_key}&s={movie_name}")

    movie_data = response.json()

    if movie_data.get("Response") == "True":
        imdb_id = movie_data["Search"][0]["imdbID"]
        movie_response = requests.get(f"http://www.omdbapi.com/?apikey={omdb_api_key}&i={imdb_id}&plot=full")
        movie_data = movie_response.json()
        title = movie_data.get("Title")
        genre = movie_data.get("Genre")
        moods = []
        for i in range(5):
            try:
                response = ask_openai(f"Provide a list of up to 3 mood tags for the following movie plot: {movie_data.get('Plot')}. Return the tags as a comma-separated list without any additional text. Mood tags reffers to right situations to watch the movie, e.g. 'Date night', 'Family movie', 'Action-packed', 'Feel-good', 'Thrilling', 'Romantic', 'Comedy', 'Horror', 'Sci-fi', 'Drama'.")
                moods += [m.strip() for m in response.split(",") if m.strip()]
                if len(moods) > 0:
                    break
            except Exception as e:
                print(f"[mood tags] error: {e}", flush=True)


        properties = {
            "Name": {"type": "title", "content": title},
            "Genre": {"type": "multi_select", "content": [g.strip() for g in genre.split(",")] if genre else []},
            "Mood": {"type": "multi_select", "content": [mood for mood in moods if mood]}
        }

        # Remove None values
        properties = {k: v for k, v in properties.items() if v is not None}

        create_notion_page(notion_client, os.environ["MOVIES_DATABASE_ID"], properties)
        return f"Added movie '{title}' to Notion database with genres {genre} and moods {', '.join(moods)}."
    else:
        raise ValueError(f"Movie not found: {movie_data.get('Error')}")

def log_movie_watch_and_rating(movie_name: str, rating: int):
    """Log a watch for an existing movie in the Notion database and update it's rating according to the user's input.
    Args:
        movie_name (str): The name of the movie to log a watch for.
        rating (int): The rating to assign to the movie (1-10).
    Returns:
        Result of the update operation.
    """
    filter = {
        "property": "Name",
        "title": {
            "equals": movie_name
        }
    }
    pages = get_notion_pages(notion_client, os.environ["MOVIES_DATABASE_ID"], filter=filter)

    if not pages:
        return "Movie not found in the Notion database."

    page_id = pages[0]["id"]
    notion_client.pages.update(
        page_id,
        properties={
            "Rating": {"type": "select", "select": {"name": '⭐' * rating}},
            "Last Watched": {"type": "date", "date": {"start": datetime.now().isoformat()}}
        }
    )
    return f"Logged watch and updated rating for '{movie_name}' to {rating} stars."

def suggest_movie(prompt: str) -> str:
    """Suggest a movie from the Notion database based on the user's prompt using OpenAI.
    Args:
        prompt (str): The user's prompt describing the type of movie they want.
    Returns:
        str: The suggested movie title.
    """
    movies = get_notion_pages(notion_client, os.environ["MOVIES_DATABASE_ID"])
    movies_data = [movie['properties'] for movie in movies]
    response = ask_openai(f"""Suggest up to 5 movies based on the following prompt: {prompt}. 
                          These are the movies properties in the database: {' | '.join(str(movie) for movie in movies_data)}, 
                          if you suggest a movie which has a 'Last Watched' parameter, please take that into account and point it out. 
                          Return a playful response.
                          The response will be sent over Slack, so make sure it's concise, engaging and match the way Slack renders messages (for bold text, use 1 asterisk).""")
    return response.strip()

def get_monthly_financial_evaluation(month: str, year: int) -> str:
    """Get a financial evaluation for a given month and year using OpenAI according to the user's financial data stored in Notion.
    Args:
        month (str): The month for which to get the financial evaluation (e.g., "January").
        year (int): The year for which to get the financial evaluation (e.g., 2023).
    Returns:
        str: The financial evaluation response from OpenAI.
    """
    filters = {
        "and": [
            {
                "property": "Date",
                "date": {
                    "after": datetime(year, datetime.strptime(month, "%B").month, 1).isoformat(),
                    "before": datetime(year, datetime.strptime(month, "%B").month + 1, 1).isoformat() if datetime.strptime(month, "%B").month < 12 else datetime(year + 1, 1, 1).isoformat()
                }
            }
        ]
    }
    expenses = get_notion_pages(notion_client, os.environ["EXPENSES_DATABASE_ID"], filter=filters)
    
def receipt_url_to_notion_with_evaluation(pdf_url: str) -> str:
    """Upload a receipt pdf to the notion expenses database and process it using OpenAI to extract relevant information and provide an evaluation.
    Args:
        pdf_url (str): The URL of the receipt pdf.
    Returns:
        str: Confirmation message after processing the receipt along with an evaluation.
    """
    file_id = upload_pdf_to_openai(pdf_url, client=openai_client)

    instructions = (
        "You extract receipts. Return STRICT JSON with keys: "
        "vendor (string), date (YYYY-MM-DD), category (string) for one of the options [Groceries 🛒, Decor 🪑], "
        "total (number), items (array of {name, qty (number|null), unit_price (number|null), line_total (number|null)}), "
        "and comment (string) with a brief evaluation of whether the spending is expensive by Israeli standards. "
        "If a value is missing, use null. Do not include extra text."
    )
    resp = openai_client.responses.create(
        model="gpt-4o",
        input=[
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": instructions},
                    {"type": "input_file", "file_id": file_id},
                ],
            }
        ],
        temperature=0.2,
    )
    # The SDK provides a convenience property for plain text output:
    clean_text = re.sub(r'^```json\s*|\s*```$', '', resp.output_text.strip())
    json_data = json.loads(clean_text)
    # Add to Notion
    properties = {
        "Description": {"type": "title", "content": json_data.get("vendor")},
        "Date": {"type": "date", "content": json_data.get("date")},
        "Amount": {"type": "number", "content": json_data.get("total")},
        "Invoice": {"type": "file", "content": {"name": f"Receipt_{json_data.get('vendor')}_{json_data.get('date')}.pdf", "url": pdf_url}},
        "Category": {"type": "multi_select", "content": ["Home 🏡"]},
        "Sub Category": {"type": "multi_select", "content": [json_data.get("category")]},
        "Tag": {"type": "multi_select", "content": ["Tal 👨🏻"]},
        "Type": {"type": "select", "content": "Need"},
        "Payment Method": {"type": "select", "content": "Credit"},
    }
    create_notion_page(notion_client, os.environ["EXPENSES_DATABASE_ID"], properties)
    open_ai_evaluation_and_confirmation = ask_openai(f"I have just added an expense to my Notion database with the following details: {json_data}. Provide a brief evaluation of the spending according to Israel's standards, and confirm that the expense has been logged.")
    return open_ai_evaluation_and_confirmation.strip()
