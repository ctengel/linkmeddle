#!/usr/bin/env python3

import linkmeddle

def orbb(url):
    soup = linkmeddle.getsoup(url)
    links = [(x.get('href'), x.contents[0]) for x in soup.find_all('a', class_='postlink')]
    for link in links:
        print('{}\t{}'.format(*link))


if __name__ == '__main__':
    linkmeddle.cli(orbb)
