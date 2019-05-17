from cffi import FFI
from string import ascii_lowercase
import os
import sys
from random import choice
from mujoco_py.utils import discover_mujoco, MISSING_KEY_MESSAGE
from os.path import exists, join
from mujoco_py.utils import manually_link_libraries, load_dynamic_ext, remove_mujoco_build
import subprocess
from subprocess import CalledProcessError
import glob
from shutil import move



def build_fn_cleanup(name):
    '''
    Cleanup files generated by building callback.
    Set the MUJOCO_PY_DEBUG_FN_BUILDER environment variable to disable cleanup.
    '''
    if not os.environ.get('MUJOCO_PY_DEBUG_FN_BUILDER', False):
        for f in glob.glob(name + '*'):
            try:
                os.remove(f)
            except PermissionError as e:
                # This happens trying to remove libraries on appveyor
                print('Error removing {}, continuing anyway: {}'.format(f, e))


def build_callback_fn(function_string, userdata_names=[]):
    '''
    Builds a C callback function and returns a function pointer int.

        function_string : str
            This is a string of the C function to be compiled
        userdata_names : list or tuple
            This is an optional list to defince convenience names

    We compile and link and load the function, and return a function pointer.
    See `MjSim.set_substep_callback()` for an example use of these callbacks.

    The callback function should match the signature:
        void fun(const mjModel *m, mjData *d);

    Here's an example function_string:
        ```
        """
        #include <stdio.h>
        void fun(const mjModel* m, mjData* d) {
            printf("hello");
        }
        """
        ```

    Input and output for the function pass through userdata in the data struct:
        ```
        """
        void fun(const mjModel* m, mjData* d) {
            d->userdata[0] += 1;
        }
        """
        ```

    `userdata_names` is expected to match the model where the callback is used.
    These can bet set on a model with:
        `model.set_userdata_names([...])`

    If `userdata_names` is supplied, convenience `#define`s are added for each.
    For example:
        `userdata_names = ['my_sum']`
    Will get gerenerated into the extra line:
        `#define my_sum d->userdata[0]`
    And prepended to the top of the function before compilation.
    Here's an example that takes advantage of this:
        ```
        """
        void fun(const mjModel* m, mjData* d) {
            for (int i = 0; i < m->nu; i++) {
                my_sum += d->ctrl[i];
            }
        }
        """
        ```
    Note these are just C `#define`s and are limited in how they can be used.

    After compilation, the built library containing the function is loaded
    into memory and all of the files (including the library) are deleted.
    To retain these for debugging set the `MUJOCO_PY_DEBUG_FN_BUILDER` envvar.

    To save time compiling, these function pointers may be re-used by many
    different consumers.  They are thread-safe and don't acquire the GIL.

    See the file `tests/test_substep.py` for additional examples,
    including an example which iterates over contacts to compute penetrations.
    '''
    assert isinstance(userdata_names, (list, tuple)), \
        'invalid userdata_names: {}'.format(userdata_names)
    ffibuilder = FFI()
    ffibuilder.cdef('extern uintptr_t __fun;')
    name = '_fn_' + ''.join(choice(ascii_lowercase) for _ in range(15))
    source_string = '#include <mujoco.h>\n'
    # Add defines for each userdata to make setting them easier
    for i, data_name in enumerate(userdata_names):
        source_string += '#define {} d->userdata[{}]\n'.format(data_name, i)
    source_string += function_string
    source_string += '\nuintptr_t __fun = (uintptr_t) fun;'
    # Link against mujoco so we can call mujoco functions from within callback
    ffibuilder.set_source(name, source_string,
                          include_dirs=[join(mujoco_path, 'include')],
                          library_dirs=[join(mujoco_path, 'bin')],
                          libraries=['mujoco200'])
    # Catch compilation exceptions so we can cleanup partial files in that case
    try:
        library_path = ffibuilder.compile(verbose=True)
    except Exception as e:
        build_fn_cleanup(name)
        raise e
    # On Mac the MuJoCo library is linked strangely, so we have to fix it here
    if sys.platform == 'darwin':
        fixed_library_path = manually_link_libraries(mujoco_path, library_path)
        move(fixed_library_path, library_path)  # Overwrite with fixed library
    module = load_dynamic_ext(name, library_path)
    # Now that the module is loaded into memory, we can actually delete it
    build_fn_cleanup(name)
    return module.lib.__fun


class ignore_mujoco_warnings:
    """
    Class to turn off mujoco warning exceptions within a scope. Useful for
    large, vectorized rollouts.
    """

    def __enter__(self):
        self.prev_user_warning = cymj.get_warning_callback()
        cymj.set_warning_callback(user_warning_ignore_exception)
        return self

    def __exit__(self, type, value, traceback):
        cymj.set_warning_callback(self.prev_user_warning)


class MujocoException(Exception):
    pass


def user_warning_raise_exception(warn_bytes):
    '''
    User-defined warning callback, which is called by mujoco on warnings.
    Here we have two primary jobs:
        - Detect known warnings and suggest fixes (with code)
        - Decide whether to raise an Exception and raise if needed
    More cases should be added as we find new failures.
    '''
    # TODO: look through test output to see MuJoCo warnings to catch
    # and recommend. Also fix those tests
    warn = warn_bytes.decode()  # Convert bytes to string
    if 'Pre-allocated constraint buffer is full' in warn:
        raise MujocoException(warn + 'Increase njmax in mujoco XML')
    if 'Pre-allocated contact buffer is full' in warn:
        raise MujocoException(warn + 'Increase njconmax in mujoco XML')
    # This unhelpfully-named warning is what you get if you feed MuJoCo NaNs
    if 'Unknown warning type' in warn:
        raise MujocoException(warn + 'Check for NaN in simulation.')
    raise MujocoException('Got MuJoCo Warning: {}'.format(warn))


def user_warning_ignore_exception(warn_bytes):
    pass


def find_key():
    ''' Try to find the key file, if missing, print out a big message '''
    if exists(key_path):
        return
    print(MISSING_KEY_MESSAGE.format(key_path), file=sys.stderr)


def activate():
    functions.mj_activate(key_path)


def compile_with_multiple_attempts():
    compile_mujoco_path = os.path.join(os.path.dirname(__file__), "compile_mujoco.py")
    for attempt in range(3):
        try:
            subprocess.check_call(["python", compile_mujoco_path], timeout=150)
            so_path = os.path.join(os.path.dirname(__file__), "generated", "*.so")
            cext_so_path = glob.glob(so_path)
            assert len(cext_so_path) == 1, ("Expecting only one .so file under " + so_path)
            cext_so_path = cext_so_path[0]
            cymj = load_dynamic_ext('cymj', cext_so_path)
            return cymj
        except (CalledProcessError, TimeoutError, ImportError) as _:
            remove_mujoco_build()  # Cleans the installation.
    raise Exception("Failed to compile mujoco_py.")


# Trick to expose all mj* functions from mujoco in mujoco_py.*
class dict2(object):
    pass


cymj = compile_with_multiple_attempts()
mujoco_path, key_path = discover_mujoco()
functions = dict2()
for func_name in dir(cymj):
    if func_name.startswith("_mj"):
        setattr(functions, func_name[1:], getattr(cymj, func_name))

# Set user-defined callbacks that raise assertion with message
cymj.set_warning_callback(user_warning_raise_exception)
