import multiprocessing
import os
from pathlib import Path
from time import sleep, time
from elasticsearch import helpers
from typing import List
from util import data_io
from util.consume_with_pool import pool_consume

from esutil.es_util import build_es_client, build_es_action


def pop_exception(d):
    if "exception" in d["index"]:
        d["index"].pop("exception")
    return d


def populate_es_parallel_pool(
    files,
    es_index_name,
    es_type,
    limit=None,
    process_fun=lambda x: x,
    num_processes=4,
    chunk_size=1_000,
    host = "localhost"
):
    STATE_INDEX_NAME = es_index_name + "_state"
    STATE_TYPE = "file_state"

    def consumer_supplier():
        es_client = build_es_client(host=host)

        def update_state(file, keyvalue):
            es_client.update(
                STATE_INDEX_NAME, file, body={"doc": keyvalue}, doc_type=STATE_TYPE,
            )

        def try_to_process(datum):
            try:
                return process_fun(datum)
            except:
                return datum

        def consumer(file):
            num_to_skip = es_client.get_source(
                index=STATE_INDEX_NAME, id=file, doc_type=STATE_TYPE
            )["line"]
            process_name = multiprocessing.current_process().name
            print(
                "%s is skipping %d lines in file: %s "
                % (process_name, num_to_skip, file)
            )

            results_g = helpers.streaming_bulk(
                es_client,
                actions=(
                    build_es_action(
                        try_to_process(d), es_index_name, es_type, op_type="index"
                    )
                    for d in data_io.read_jsonl(
                        file, limit=limit, num_to_skip=num_to_skip
                    )
                ),
                chunk_size=chunk_size,
                yield_ok=True,
                raise_on_error=False,
                raise_on_exception=False,
            )
            counter = num_to_skip
            for k, (ok, d) in enumerate(results_g):
                counter += 1
                if not ok and "index" in d:
                    print("shit")
                if k % 1000 == 0:
                    update_state(file, {"line": counter})

            update_state(file, {"line": counter})
            if limit is None or counter < limit:
                update_state(file, {"done": True})

            print(
                "%s is done; inserted %d new docs!"
                % (process_name, counter - num_to_skip)
            )

        return consumer

    if num_processes > 1:
        pool_consume(
            data=files, consumer_supplier=consumer_supplier, num_processes=num_processes
        )
    else:
        consumer = consumer_supplier()
        [consumer(file) for file in files]


def setup_index(
    es_client, files: List[str], INDEX_NAME, TYPE, from_scratch=False, mapping=None
):
    STATE_INDEX_NAME = INDEX_NAME + "_state"
    STATE_TYPE = "file_state"

    if from_scratch:
        es_client.indices.delete(index=INDEX_NAME, ignore=[400, 404])
        es_client.indices.delete(index=STATE_INDEX_NAME, ignore=[400, 404])

    sleep(3)
    es_client.indices.create(index=INDEX_NAME, ignore=400, body=mapping)
    es_client.indices.create(index=STATE_INDEX_NAME, ignore=400)
    sleep(3)

    def build_es_action(datum, index_name, es_type, op_type="index"):
        _source = {
            k: None if isinstance(v, str) and len(v) == 0 else v
            for k, v in datum.items()
        }
        doc = {
            "_id": datum["file"],
            "_op_type": op_type,
            "_index": index_name,
            "_type": es_type,
            "_source": _source,
        }
        return doc

    helpers.bulk(
        es_client,
        (
            build_es_action(
                {"file": file, "line": 0, "done": False},
                STATE_INDEX_NAME,
                STATE_TYPE,
                op_type="create",
            )
            for file in files
        ),
        raise_on_error=False,
    )

    sum_in_state = sum(
        [
            es_client.get_source(index=STATE_INDEX_NAME, id=file, doc_type=STATE_TYPE)[
                "line"
            ]
            for file in files
        ]
    )
    if sum_in_state > 0:
        count = es_client.count(index=INDEX_NAME, doc_type=TYPE)["count"]
        if sum_in_state != count:
            print(sum_in_state)
            print(count)
            assert False

    body = '''
            {
              "query": {
                "bool": {
                  "must": [
                    {"term": {
                      "done": {
                        "value": "true"
                      }
                    }}
                  ]
                }
              }
            }    
    '''
    r = es_client.search(index=STATE_INDEX_NAME, body=body,size=10_000)

    files_in_es = set([os.path.split(s['_source']['file'])[1] for s in r['hits']['hits']])
    not_yet_in_index_files = [f for f in files if os.path.split(f)[1] not in files_in_es]
    print('got %d files which are not yet in ES-index'%len(not_yet_in_index_files))
    return not_yet_in_index_files


if __name__ == "__main__":

    def get_files():
        home = str(Path.home())
        path = home + "/data/semantic_scholar"
        files = [
            path + "/" + file_name
            for file_name in os.listdir(path)
            if file_name.startswith("s2") and file_name.endswith(".gz")
        ]
        return files

    INDEX_NAME = "test-parallel-pool"
    TYPE = "paper"

    es = build_es_client()

    files = get_files()

    files = setup_index(es, files, INDEX_NAME, TYPE, from_scratch=False)

    start = time()
    num_processes = 8
    populate_es_parallel_pool(
        files, INDEX_NAME, TYPE, limit=10_000, num_processes=num_processes
    )
    dur = time() - start

    sleep(5)
    count = es.count(index=INDEX_NAME, doc_type=TYPE)["count"]
    print(
        "populating %s now containing %d documents took: %0.2f seconds"
        % (INDEX_NAME, count, dur)
    )
