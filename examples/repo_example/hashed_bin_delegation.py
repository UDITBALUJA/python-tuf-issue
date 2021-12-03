"""
A TUF hash bin delegation example using the low-level TUF Metadata API.

As 'repository_tool' and 'repository_lib' are being deprecated, hash bin
delegation interfaces are no longer available in this implementation. The
example code in this file demonstrates how to easily implement those
interfaces, and how to use them together with the TUF metadata API, to perform
hash bin delegation.

Contents:
- Re-usable hash bin delegation helpers
- Basic hash bin delegation example

See 'basic_repo.py' for a more comprehensive TUF metadata API example.

NOTE: Metadata files will be written to a 'tmp*'-directory in CWD.

"""
import hashlib
import os
import tempfile
from collections import OrderedDict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple

from securesystemslib.keys import generate_ed25519_key
from securesystemslib.signer import SSlibSigner

from tuf.api.metadata import (
    DelegatedRole,
    Delegations,
    Key,
    Metadata,
    TargetFile,
    Targets,
)
from tuf.api.serialization.json import JSONSerializer


def _in(days: float) -> datetime:
    """Adds 'days' to now and returns datetime object w/o microseconds."""
    return datetime.utcnow().replace(microsecond=0) + timedelta(days=days)


SPEC_VERSION = "1.0.19"
roles: Dict[str, Metadata] = {}
keys: Dict[str, Dict[str, Any]] = {}

# Hash bin delegation
# ===================
# Hash bin delegation allows to automatically distribute a large number of
# target files over a fixed number of targets metadata, in order to reduce the
# size of the specific targets metadata a client needs to download for a given
# target file, and thus reduce the metadata network overhead caused by TUF.
#
# It is achieved by uniformly delegating the responsibility for target files
# based on the leading digits of the hash of their file path, where each
# delegated targets role is responsible for an incremental range of target path
# hash prefixes.
#
# The only number that needs to be configured is the number of bins, everything
# else is derived using the mathematical operations outlined below. An
# appropriate number of bins depends on the expected number of target files in
# a repository. For the purpose of this example we choose...

# The fixed number of bins determines the length of any considered hash prefix,
# how many prefixes exist in total, and how many prefixes fall into each bin.
NUMBER_OF_BINS = 32  # power of 2 for even distribution of hash prefixes

# The available digits in the hexadecimal representation of the number of bins
# (minus one, counting starts at zero) determines the length of any hash prefix,
# i.e. how many left digits need to be considered to assign the hash to a bin.
PREFIX_LEN = len(f"{NUMBER_OF_BINS - 1:x}")  # 2

# The total number of distinct hash prefixes is determined by the highest
# hexadecimal number that can be represented with the given number of digits.
NUMBER_OF_PREFIXES = 16 ** PREFIX_LEN  # 256

# If the number of bins is a power of two, all hash prefixes can be evenly
# distributed over all bins and thus allow us to compute the size of any bin.
BIN_SIZE = NUMBER_OF_PREFIXES // NUMBER_OF_BINS  # 8


# Helpers
# -------
def _bin_name(low: int, high: int) -> str:
    """Generates a bin name according to the hash prefixes the bin serves.

    The name is either a single hash prefix for bin size 1, or a range of hash
    prefixes otherwise. The prefix length is needed to zero-left-pad the
    hex representation of the hash prefix for uniform bin name lengths.
    """
    if low == high:
        return f"{low:0{PREFIX_LEN}x}"

    return f"{low:0{PREFIX_LEN}x}-{high:0{PREFIX_LEN}x}"


def generate_hash_bins() -> Iterator[Tuple[str, List[str]]]:
    """Returns generator for bin names and hash prefixes per bin."""
    # Iterate over the total number of hash prefixes in 'bin size'-steps to
    # generate bin names and a list of hash prefixes served by each bin.
    for low in range(0, NUMBER_OF_PREFIXES, BIN_SIZE):
        high = low + BIN_SIZE - 1
        bin_name = _bin_name(low, high)
        hash_prefixes = []
        for prefix in range(low, low + BIN_SIZE):
            hash_prefixes.append(f"{prefix:0{PREFIX_LEN}x}")

        yield bin_name, hash_prefixes


def find_hash_bin(path: str) -> str:
    """Returns name of bin for target file based on the target path hash."""
    # Generate hash digest of passed target path and take its prefix, given the
    # global prefix length for the given number of bins.
    hasher = hashlib.sha256()
    hasher.update(path.encode("utf-8"))
    target_name_hash = hasher.hexdigest()
    prefix = int(target_name_hash[:PREFIX_LEN], 16)
    # Find lower and upper bounds for hash prefix given its numerical value and
    # the the general bin size for the given number of bins.
    low = prefix - (prefix % BIN_SIZE)
    high = low + BIN_SIZE - 1
    return _bin_name(low, high)


# Keys
# ----
# Given that the primary concern of hash bin delegation is to reduce network
# overhead, it is acceptable to re-use one signing key for all delegated
# targets roles (bin-n). However, we do use a different key for the delegating
# targets role (bins). Considering the high responsibility but also low
# volatility of the bins role, it is recommended to require signature
# thresholds and keep the keys offline in a real-world scenario.

# NOTE: See "Targets delegation" and "Signature thresholds" paragraphs in
# 'basic_repo.py' for more details
for name in ["bin-n", "bins"]:
    keys[name] = generate_ed25519_key()


# Targets roles
# -------------
# NOTE: See "Targets" and "Targets delegation" paragraphs in 'basic_repo.py'
# example for more details about the Targets object.

# Create preliminary delegating targets role (bins) and add public key for
# delegated targets (bin_n) to key store. Delegation details are update below.
roles["bins"] = Metadata[Targets](
    signed=Targets(
        version=1,
        spec_version=SPEC_VERSION,
        expires=_in(365),
        targets={},
        delegations=Delegations(
            keys={
                keys["bin-n"]["keyid"]: Key.from_securesystemslib_key(
                    keys["bin-n"]
                )
            },
            roles=OrderedDict(),
        ),
    ),
    signatures=OrderedDict(),
)

# The hash bin generator yields an ordered list of incremental hash bin names
# (ranges), plus the hash prefixes each bin is responsible for, e.g.:
#
# bin_n_name:  00-07  bin_n_hash_prefixes: 00 01 02 03 04 05 06 07
#              08-0f                       08 09 0a 0b 0c 0d 0e 0f
#              10-17                       10 11 12 13 14 15 16 17
#              ...                         ...
#              f8-ff                       f8 f9 fa fb fc fd fe ff
for bin_n_name, bin_n_hash_prefixes in generate_hash_bins():
    # Update delegating targets role (bins) with delegation details for each
    # delegated targets role (bin_n).
    roles["bins"].signed.delegations.roles[bin_n_name] = DelegatedRole(
        name=bin_n_name,
        keyids=[keys["bin-n"]["keyid"]],
        threshold=1,
        terminating=False,
        path_hash_prefixes=bin_n_hash_prefixes,
    )

    # Create delegated targets roles (bin_n)
    roles[bin_n_name] = Metadata[Targets](
        signed=Targets(
            version=1, spec_version=SPEC_VERSION, expires=_in(7), targets={}
        ),
        signatures=OrderedDict(),
    )

# Add target file
# ---------------
# For the purpose of this example we will protect the integrity of this very
# example script by adding its file info to the corresponding bin metadata.

# NOTE: See "Targets" paragraph in 'basic_repo.py' example for more details
# about adding target file infos to targets metadata.
local_path = Path(__file__).resolve()
target_path = f"{local_path.parts[-2]}/{local_path.parts[-1]}"
target_file_info = TargetFile.from_file(target_path, str(local_path))

# The right bin for a target file is determined by the 'target_path' hash, e.g.:
#
# target_path:                      'repo_example/hashed_bin_delegation.py'
# target_path (hash digest):        '85e1a6c06305bd9c1e15c7ae565fd16ea304bfc...'
#
# --> considered hash prefix '85', falls into bin '80-87'
bin_for_target = find_hash_bin(target_path)
roles[bin_for_target].signed.targets[target_path] = target_file_info


# Sign and persist
# ----------------
# Sign all metadata and persist to temporary directory at CWD for review
# (most notably see 'bins.json' and '80-87.json').

# NOTE: See "Persist metadata" paragraph in 'basic_repo.py' example for more
# details about serialization formats and metadata file name convention.
PRETTY = JSONSerializer(compact=False)
TMP_DIR = tempfile.mkdtemp(dir=os.getcwd())

for role_name, role in roles.items():
    key = keys["bins"] if role_name == "bins" else keys["bin-n"]
    signer = SSlibSigner(key)
    role.sign(signer)

    filename = f"{role_name}.json"
    filepath = os.path.join(TMP_DIR, filename)
    role.to_file(filepath, serializer=PRETTY)
