import http.cookiejar
import http.cookies

def convert_header_to_cookies_txt(header_string, output_file):
    # Create a new cookie jar
    cookie_jar = http.cookiejar.MozillaCookieJar()

    # Parse the header string and populate the cookie jar
    cookies = http.cookies.SimpleCookie()
    cookies.load(header_string)
    for key, morsel in cookies.items():
        print(key, morsel)
        # Create a Cookie object and add it to the jar
        cookie = http.cookiejar.Cookie(
            version=0,
            name=key,
            value=morsel.value,
            port=None,
            port_specified=False,
            domain='',
            domain_specified=False,
            domain_initial_dot=False,
            path='/',
            path_specified=True,
            secure=False,
            expires=None,
            discard=False,
            comment=None,
            comment_url=None,
            rest={'HttpOnly': None},
            rfc2109=False
        )
        cookie_jar.set_cookie(cookie)
    print(cookie_jar)
    # Save the cookies to a cookies.txt file
    cookie_jar.save(output_file, ignore_discard=True)
