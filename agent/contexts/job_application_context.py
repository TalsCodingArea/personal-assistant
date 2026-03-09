"""
Job Application Context (agent/contexts/job_application_context.py)
===================================================================

This context is used by the intent router to detect job application requests
in the personal assistant chat. It is NOT used to build a conversational agent
for this workflow — the job application pipeline is a direct sequential function
call in app.py (see _handle_job_application).

Intent detection: a message is classified as "job_application" when the user
sends a job listing URL, optionally preceded by a trigger phrase such as:
  "apply https://..."
  "job https://..."
  "apply for this: https://..."
  or just a bare job board URL

The pipeline is defined in tools/job_tools.py → run_job_application_workflow().
"""

JOB_APPLICATION_CONTEXT = """
Job Application mode:

The user has provided a job listing URL. Execute the job application workflow:

1. Acknowledge the URL and let the user know the pipeline has started.
2. The system will automatically:
   - Scrape the job listing
   - Research the company
   - Generate a tailored resume and cover letter (PDF)
   - Write a personal note
   - Log the application to Notion
   - Return all materials via Telegram
3. No further user input is needed during the pipeline.
4. If the scraping fails or the page is JavaScript-rendered (e.g. LinkedIn),
   inform the user and ask them to paste the job description text manually.
"""
