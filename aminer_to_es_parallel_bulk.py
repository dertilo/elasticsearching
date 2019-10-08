import os
from time import sleep, time

from elasticsearch import helpers
from tqdm import tqdm
from util import data_io
from util.data_io import read_jsonl

from es_util import build_es_action, build_es_client

def pop_exception(d):
    d['index'].pop('exception')
    return d

def populate_es_parallel_bulk(es, files, es_index_name, es_type, limit=None, num_processes = 4):
    dicts_g = (d for file in files for d in read_jsonl(file,limit=limit))

    actions_g = (build_es_action(d,es_index_name,es_type) for d in dicts_g)
    results_g = helpers.parallel_bulk(es, actions_g, thread_count=num_processes, queue_size=num_processes,
                                      chunk_size=1000,
                                      raise_on_exception=False,
                                      raise_on_error=False)
    failed_g = (pop_exception(d) for ok,d in tqdm(results_g) if not ok)
    data_io.write_jsonl('failed.jsonl',failed_g)


if __name__ == "__main__":
    INDEX_NAME = "test"
    TYPE = "paper"
    es = build_es_client()

    es.indices.delete(index=INDEX_NAME, ignore=[400, 404])
    es.indices.create(index=INDEX_NAME, ignore=400)
    sleep(3)

    path = '/docker-share/data/MAG_papers'
    start = time()
    files = [path + '/' + file_name for file_name in os.listdir(path) if file_name.endswith('txt.gz')]
    populate_es_parallel_bulk(es, files, INDEX_NAME, TYPE, limit=2000_000)
    dur = time()-start

    sleep(3)
    count = es.count(index=INDEX_NAME, doc_type=TYPE, body={"query": {"match_all": {}}})['count']
    print("populating es-index of %d documents took: %0.2f seconds"%(count,dur))
