from google.oauth2.service_account import Credentials
import gspread, json

info = json.load(open("casa-do-pao-frances-api-7ca080458287.json"))
creds = Credentials.from_service_account_info(
    info,
    scopes=["https://www.googleapis.com/auth/spreadsheets"]
)
gc = gspread.authorize(creds)
sh = gc.open_by_key("1tJW5BHQTq3a5O1w-RTIJ7iIUcq29939NBsLgKMyGCEk")
print(sh.title)
