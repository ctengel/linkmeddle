#!/usr/bin/env python3

"""Tools to deal with cookiex.txt files

See cli() function for CLI doc
"""

import http.cookiejar
import http.cookies
import sys

def convert_header_to_cookies_txt(header_string: str, output_file: str) -> None:
    """Converts cookies header to cookies.txt

    header_string is the HTTP Cookie: header value string from client request
    output_file is the name of a new netscape cookies.txt to generate"""

    # Create a new cookie jar
    cookie_jar = http.cookiejar.MozillaCookieJar()

    # Parse the header string and populate the cookie jar
    cookies = http.cookies.SimpleCookie()
    cookies.load(header_string)
    for key, morsel in cookies.items():
        #print(key, morsel)
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
    #print(cookie_jar)
    # Save the cookies to a cookies.txt file
    cookie_jar.save(output_file, ignore_discard=True)

def cli():
    """CLI to take curl and generate cookies.txt

    First arg is the cookies.txt filename
    Rest of args is a curl command
    Can use firefox "copy to CURL" feature
    """
    cookiefile = sys.argv[1]
    assert sys.argv[2] == 'curl'
    for arg in sys.argv[3:]:
        if arg.startswith('Cookie: '):
            cookieinput = arg.removeprefix('Cookie: ')
            #print(cookieinput, cookiefile)
            convert_header_to_cookies_txt(cookieinput, cookiefile)
            return
    assert False

if __name__ == '__main__':
    cli()
