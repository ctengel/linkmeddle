"""Given a CSV file and an archive, output archive with only those which are not missing"""

import csv
import click

@click.command()
@click.argument('csvfile')
@click.argument('inarc')
@click.argument('outarc')
def rm_missing(csvfile, inarc, outarc):
    """Read local CSV and filter archive"""
    with open(csvfile, newline='') as cfh:
        reader = csv.DictReader(cfh)
        nonexistant = [(x['extractor_key'], x['id'])
                       for x in reader
                       if x['in_archive'] == 'True' and not x['mediafile']]
    with open(inarc) as ifh:
        with open(outarc, 'w') as ofh:
            for line in ifh:
                if tuple(line.split()) in nonexistant:
                    print("OMIT {}".format(line.strip()))
                else:
                    print("KEEP {}".format(line.strip()))
                    ofh.write(line)


if __name__ == '__main__':
    rm_missing()
