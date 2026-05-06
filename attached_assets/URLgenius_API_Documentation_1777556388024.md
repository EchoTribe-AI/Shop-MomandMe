URLgenius API Documentation
Create Your Links Programmatically
REST Web Services
The URLgenius REST API allows you to query meta-data about your deeplinks. You can view this information both inside your dashboard and from within the API. Creating deeplinks is easy.

Since the API is based on REST principles, it's very easy to write and test applications. You can use your browser to access URLs, and you can use pretty much any HTTP client in any programming language to interact with the API.

API Access
Click here to get your API key. You need to have a Credit Card on file in order to start using it.

We limit the API to 2 requests per second.

Versioning
Breaking changes to the API will be rolled out via versions in the URL path.

CURRENT VERSION: v2

Authentication
Access to the API end points is handled by way of an authorization header. All requests which come into the API must have the appropriate api-key header. Any actions initiated by the API with the account token can affect deeplinks created via the UI and visa versa.

Include the authorization header "api-key" and set it to your account api key.

Key	Value
api-key	your_api_key
Base URL
All URLs referenced in the documentation have the following base:

https://api.urlgeni.us/

The URLgenius REST API is served over HTTPS. To ensure data privacy, unencrypted HTTP is not supported.

A Deeplink Resource
A link resource is the core of the URLgenius API and represents a deeplink into a mobile application. Once a deeplink is created, it is added to the account of the token holder. You can retrieve statistics about that deeplink, delete the deeplink, get a list of deeplinks, and of course create them with a single parameter.

POST
Create a Link with App Store Fallback
https://api.urlgeni.us/api/v2/links?
AUTHORIZATION
API Key
Key
api-key

Value
<value>

HEADERS
Content-Type
application/json

PARAMS
Body
raw
{"url":"https://example.com", "fallback_app_store":true}
Example Request
Create a Link with App Store Fallback
View More
python
import http.client
import json

conn = http.client.HTTPSConnection("api.urlgeni.us")
payload = json.dumps({
  "url": "https://example.com",
  "fallback_app_store": True
})
headers = {
  'Content-Type': 'application/json',
  'api-key': '<YOUR-API-KEY>'
}
conn.request("POST", "/api/v2/links", payload, headers)
res = conn.getresponse()
data = res.read()
print(data.decode("utf-8"))
Example Response
Body
Headers (0)
No response body
This request doesn't return any response body
POST
Create a Link with Embedded Browser
https://api.urlgeni.us/api/v2/links?external_browser=true
AUTHORIZATION
API Key
Key
api-key

Value
<value>

HEADERS
Content-Type
application/json

PARAMS
external_browser
true

Body
raw
{"url":"https://www.example.com/app"}
Example Request
Create a Link with Embedded Browser
View More
python
import http.client
import json

conn = http.client.HTTPSConnection("api.urlgeni.us")
payload = json.dumps({
  "url": "https://www.example.com/app"
})
headers = {
  'Content-Type': 'application/json'
}
conn.request("POST", "/api/v2/links?external_browser=true", payload, headers)
res = conn.getresponse()
data = res.read()
print(data.decode("utf-8"))
Example Response
Body
Headers (0)
No response body
This request doesn't return any response body
DELETE
DELETE Link
https://api.urlgeni.us/api/v2/links/<LINK-ID>
To remove a deeplink from your account, send an HTTP DELETE request to this end point

AUTHORIZATION
API Key
Key
api-key

Value
<value>

Example Request
DELETE Link
python
import http.client

conn = http.client.HTTPSConnection("api.urlgeni.us")
payload = ''
headers = {
  'api-key': '<YOUR-API-KEY>'
}
conn.request("DELETE", "/api/v2/links/21820739", payload, headers)
res = conn.getresponse()
data = res.read()
print(data.decode("utf-8"))
200 OK
Example Response
Body
Headers (23)
json
{}