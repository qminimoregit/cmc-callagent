from google import genai
from dotenv import load_dotenv
load_dotenv()
client = genai.Client()
for m in client.models.list():
    if getattr(m, 'supported_actions', None):
        # m.supported_actions might be a list of strings
        if "bidiGenerateContent" in m.supported_actions:
            print(m.name)
