"""
 Copyright European Organization for Nuclear Research (CERN)

 Licensed under the Apache License, Version 2.0 (the "License");
 You may not use this file except in compliance with the License.
 You may obtain a copy of the License at
 http://www.apache.org/licenses/LICENSE-2.0

 Authors:
 - Vitjan Zavrtanik, <vitjan.zavrtanik@gmail.com>, 2017

Daemon for distributing sonar test files to available RSE's
"""


import glob
import os
import subprocess
import sys
import threading
import logging
import time

from rucio.client.client import Client
from rucio.common.config import config_get
from rucio.common.exception import DuplicateRule
from rucio.common.exception import ReplicationRuleCreationTemporaryFailed
from rucio.common.exception import RSEBlacklisted
from rucio.common.exception import InsufficientAccountLimit


GRACEFUL_STOP = threading.Event()
logging.basicConfig(stream=sys.stdout,
                    level=getattr(logging, config_get('common', 'loglevel').upper()),
                    format='%(asctime)s\t%(process)d\t%(levelname)s\t%(message)s')


def rename_files(tdir, pattern, new_name):
    """
    Renames the files in the dataset according to the RSE
    on which the dataset is being replicated.
    """
    for cnt, file_name in enumerate(glob.iglob(os.path.join(tdir, pattern))):
        logging.info(file_name)
        logging.info(new_name + str(cnt) + '.rnd')
        if not os.path.isfile(os.path.join(tdir, new_name + str(cnt) + '.rnd')):
            logging.info("renaming..")
            os.rename(file_name, os.path.join(tdir, new_name + str(cnt) + '.rnd'))


def distribute_files(client, data_dir='small_sonar_dataset', dataset_prefix='sonar.test.small.', scope='user.vzavrtan', num_files=1):
    """
    Check whether the RSE's already containt their respective sonar test dataset
    and distributes the dataset to the ones that do not. Also checks whether the
    RSE's are available for distribution.

    param: data_dir - path to the folder which contains the dataset
    param: dataset_prefix - the prefix of the dataset ex. sonar.test.small.AGLT2_SCRATCHDISK = prefix.RSE
    param: num_files - number of files in the dataset
    """
    logging.info("Running disribution iteration")
    # remove the "if '_SCRATCHDISK'" for use on other RSE's
    endpoint_names = [x['rse'] for x in client.list_rses() if '_SCRATCHDISK' in x['rse'] and x['availability'] == 7]
    ready = []
    rules = client.list_account_rules(account='vzavrtan')
    for rule in rules:
        if dataset_prefix in rule['name'] and rule['rse_expression'] in rule['name'] and rule['state'] == 'OK' and rule['locks_ok_cnt'] == num_files:
            ready.append(rule['rse_expression'])

    ready = list(set(ready))
    for site in endpoint_names:
        if GRACEFUL_STOP.is_set():
            break
        if site not in ready:
            rename_files(data_dir, '*.rnd', dataset_prefix + site + '.file')
            msg = "Uploading to %s " % (site)
            logging.info(msg)
            process = subprocess.Popen(['rucio', 'upload', data_dir, '--rse', site], stdout=subprocess.PIPE)
            process.communicate()
            msg = "Adding dataset %s " % (dataset_prefix + site)
            logging.info(msg)
            try:
                client.add_dataset('user.vzavrtan', dataset_prefix + site)
            except Exception as exception:
                logging.warning("Error adding dataset: " + str(exception))
            for file_name in glob.iglob(os.path.join(data_dir, '*.rnd')):
                logging.info('Attaching to dataset:' + dataset_prefix + site + ' ' + scope + ':' + os.path.basename(file_name))
                try:
                    client.attach_dids(scope, dataset_prefix + site, [{'scope': scope, 'name': os.path.basename(file_name)}])
                except Exception as exception:
                    logging.warning('Error attaching dids: ' + str(exception))
            logging.info('Adding rule for dataset')
            try:
                client.add_replication_rule([{'scope': scope, 'name': dataset_prefix + site}], 1, site)
            except (DuplicateRule, RSEBlacklisted, ReplicationRuleCreationTemporaryFailed, InsufficientAccountLimit) as exception:
                msg = 'Error adding replication rule: %s' % (str(exception))
                logging.warning(msg)
        else:
            msg = "%s is already replicated." % (site)
            logging.info(msg)


def run_distribution():
    """
    Every x hours tries to distribute the datasets to RSE's that are
    missing them.
    """
    client = Client()
    counter = 0
    dataset_dir = config_get('sonar', 'dataset_dir')
    dataset_prefix = config_get('sonar', 'dataset_prefix')
    scope = config_get('sonar', 'scope')
    num_files = 10
    while not GRACEFUL_STOP.is_set():
        if counter % 12 == 0:
            distribute_files(client, data_dir=dataset_dir, dataset_prefix=dataset_prefix, scope=scope, num_files=num_files)
        time.sleep(3600)
        counter += 1


def run():
    """
    Runs the distribution daemon
    """
    thread = threading.Thread(target=run_distribution, kwargs={})
    thread.start()
    while thread and thread.isAlive():
        thread.join(timeout=3.14)


def stop(signum=None, frame=None):
    """
    Stops the distribution daemon
    """
    log_msg = 'Stopping distribution daemon: %s %s' % (signum, frame)
    logging.info(log_msg)
    GRACEFUL_STOP.set()
