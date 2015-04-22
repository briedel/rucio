"""
   Copyright European Organization for Nuclear Research (CERN)

  Licensed under the Apache License, Version 2.0 (the "License");
  You may not use this file except in compliance with the License.
  You may obtain a copy of the License at
  http://www.apache.org/licenses/LICENSE-2.0

  Authors:
  - Vincent Garonne, <vincent.garonne@cern.ch>, 2015
"""

from re import match, compile, error
from sqlalchemy.exc import IntegrityError
from traceback import format_exc

from dogpile.cache import make_region
from dogpile.cache.api import NO_VALUE

from rucio.common.exception import Duplicate, RucioException, InvalidObject
from rucio.db import models
from rucio.db.constants import KeyType
from rucio.db.session import read_session, transactional_session


REGION = make_region().configure('dogpile.cache.memcached',
                                 expiration_time=3600,
                                 arguments={'url': "127.0.0.1:11211",
                                            'distributed_lock': True})


@transactional_session
def add_naming_convention(scope, regexp, convention_type, session=None):
    """
    add a naming convention for a given scope

    :param scope: the name for the scope.
    :param regexp: the regular expression to validate the name.
    :param convention_type: the did_type on which the regexp should apply.
    :param session: The database session in use.
    """
    # validate the regular expression
    try:
        compile(regexp)
    except error:
        raise RucioException('Invalid regular expression %s!' % regexp)

    new_convention = models.NamingConvention(scope=scope,
                                             regexp=regexp,
                                             convention_type=convention_type)
    try:
        new_convention.save(session=session)
    except IntegrityError:
        raise Duplicate('Naming convention already exists!')
    except:
        raise RucioException(str(format_exc()))


@read_session
def get_naming_convention(scope, convention_type, session=None):
    """
    Get the naming convention for a given scope

    :param scope: the name for the scope.
    :param convention_type: the did_type on which the regexp should apply.
    :param session: The database session in use.

    :returns: the regular expression.
    """

    query = session.query(models.NamingConvention.regexp).\
        filter(models.NamingConvention.scope == scope).\
        filter(models.NamingConvention.convention_type == convention_type)
    for row in query:
        return row[0]


@read_session
def list_naming_conventions(session=None):
    """
    List all naming conventions.

    :param session: The database session in use.

    :returns: a list of dictionaries.
    """
    query = session.query(models.NamingConvention.scope,
                          models.NamingConvention.regexp)
    return [row._asdict() for row in query]


@read_session
def validate_name(scope, name, did_type, session=None):
    """
    Validate a name according to a naming convention.

    :param scope: the name for the scope.
    :param name: the name.
    :param did_type: the type of did.

    :param session: The database session in use.

    :returns: a dictionary with metadata.
    """

    # Check if naming convention can be found in cache region
    regexp = REGION.get(scope)
    if regexp is NO_VALUE:  # no cached entry found
        regexp = get_naming_convention(scope=scope,
                                       convention_type=KeyType.DATASET,
                                       session=session)
        regexp and REGION.set(scope, regexp)

    if not regexp:
        return
    # Valide with regexp
    groups = match(regexp, str(name))
    if groups:
        return groups.groupdict()
    raise InvalidObject("Provided name %(name)s doesn't the naming convention %(regexp)s" % locals())
