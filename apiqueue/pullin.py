import time
import click
import tasks

@click.command()
def pullin():
    lsdir = tasks.lsdir.delay()
    infotasks = [tasks.infofiledata.delay(x) for x in lsdir.get() if x.endswith('.json')]
    addtasks = []
    for task in infotasks:
        result = task.get()
        if not tasks.check_archive.delay(result).get():
            # TODO verify that the file is actually done downloading...
            # TODO way to add without info.json?
            print('adding {} {} {} {}...'.format(result.get('_filename'), result.get('webpage_url'), result.get('extractor'), result.get('id')))
            addtasks.append(tasks.add_archive.delay(result))
    for fin in addtasks:
        assert fin.get() is None


if __name__ == '__main__':
    pullin()

