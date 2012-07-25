# Copyright European Organization for Nuclear Research (CERN)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# You may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#              http://www.apache.org/licenses/LICENSE-2.0
#
# Authors:
# - Angelos Molfetas, <angelos.molfetas@cern.ch>, 2011
# - Vincent Garonne,  <vincent.garonne@cern.ch> , 2011

from rucio.core import permission


def has_permission(accountName, action, kwargs):
    """
    Checks if an account has the specified permission to
    execute an action with parameters.

    :param accountName: Account identifier.
    :param action:  The action(API call) called by the account.
    :param kwargs: List of arguments for the action.
    :returns: True if account is allowed to call the API call, otherwise False
    """
    return permission.has_permission(accountName=accountName, action=action, kwargs=kwargs)
