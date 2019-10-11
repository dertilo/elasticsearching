import multiprocessing
import os
from time import sleep, time
from elasticsearch import helpers
from util import data_io
from util.consume_with_pool import pool_consume

from es_util import build_es_action, build_es_client


def pop_exception(d):
    if 'exception' in d['index']:
        d['index'].pop('exception')
    return d

def populate_es_parallel_pool(files, es_index_name, es_type, limit=None, num_processes = 4):

    def consumer_supplier():
        es_client = build_es_client()

        def consumer(file):
            num_to_skip = es.get_source(index=STATE_INDEX_NAME, id=file, doc_type=STATE_TYPE)['line']
            print('%s is skipping %d lines in file: %s '%(multiprocessing.current_process(),num_to_skip,file))
            dicts_g = (d for d in data_io.read_jsonl(file, limit=limit, num_to_skip=num_to_skip))

            actions_g = (build_es_action(d, es_index_name, es_type, op_type='index') for d in dicts_g)
            results_g = helpers.streaming_bulk(es_client, actions_g,
                                               chunk_size=1_000,
                                               yield_ok=True,
                                               raise_on_error=False,
                                               raise_on_exception=False)
            counter = num_to_skip
            for k, (ok, d) in enumerate(results_g):
                counter+=1
                if not ok and 'index' in d:
                    print('shit')
                if k%1000==0:
                    es_client.update(STATE_INDEX_NAME, file, body={'doc': {'line': counter}}, doc_type=STATE_TYPE)

            es_client.update(STATE_INDEX_NAME, file, body={'doc': {'line': counter}}, doc_type=STATE_TYPE)
            if limit is None or counter<limit:
                es_client.update(STATE_INDEX_NAME, file, body={'doc': {'done':True}}, doc_type=STATE_TYPE)
            print('%s is done; inserted %d new docs!'%(multiprocessing.current_process(),counter-num_to_skip))

        return consumer

    if num_processes>1:
        pool_consume(
            data=files,
            consumer_supplier=consumer_supplier,
            num_processes=num_processes)
    else:
        consumer= consumer_supplier()
        [consumer(file) for file in files]

def setup_index(es_client, files, from_scratch):

    if from_scratch:
        es_client.indices.delete(index=INDEX_NAME, ignore=[400, 404])
        es_client.indices.delete(index=STATE_INDEX_NAME, ignore=[400, 404])

    es_client.indices.create(index=STATE_INDEX_NAME, ignore=400)
    es_client.indices.create(index=INDEX_NAME, ignore=400)
    sleep(3)

    def build_es_action(datum, index_name, es_type, op_type='index'):
        _source = {k: None if isinstance(v, str) and len(v) == 0 else v for k, v in datum.items()}
        doc = {
            '_id': datum['file'],
            '_op_type': op_type,
            '_index': index_name,
            '_type': es_type,
            '_source': _source
        }
        return doc

    helpers.bulk(es_client,(build_es_action({'file':file,'line':0,'done':False},STATE_INDEX_NAME,STATE_TYPE,op_type='create') for file in files),raise_on_error=False)

    sum_in_state=sum([es_client.get_source(index=STATE_INDEX_NAME, id=file, doc_type=STATE_TYPE)['line'] for file in files])
    count = es.count(index=INDEX_NAME, doc_type=TYPE)['count']
    if sum_in_state != count:
        print(sum_in_state)
        print(count)
        assert False

if __name__ == "__main__":
    INDEX_NAME = "s2papers"
    TYPE = "paper"

    STATE_INDEX_NAME = INDEX_NAME+'_state'
    STATE_TYPE = 'file_state'
    es = build_es_client()
    path = '/docker-share/data/semantic_scholar'
    files = [path + '/' + file_name for file_name in os.listdir(path) if file_name.startswith('s2') and file_name.endswith('.gz')]
    setup_index(es, files, from_scratch=False)

    start = time()
    num_processes = 8
    populate_es_parallel_pool(files, INDEX_NAME, TYPE,limit=None,num_processes=num_processes)
    dur = time()-start

    sleep(5)
    count = es.count(index=INDEX_NAME, doc_type=TYPE)['count']
    count = es.count(index=INDEX_NAME, doc_type=TYPE)['count']
    print("populating %s now containing %d documents took: %0.2f seconds"%(INDEX_NAME,count,dur))