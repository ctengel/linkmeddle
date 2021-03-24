import time
import click
import tasks

@click.command()
@click.argument('url')
def dl(url):
    result = tasks.download.delay(url)
    while not result.ready():
        print('waiting...')
        time.sleep(5)
    print(result.get())

if __name__ == '__main__':
    dl()

