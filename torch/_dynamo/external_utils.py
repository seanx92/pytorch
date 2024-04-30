# This module contains functions that *will be allowed* by dynamo

import functools

import threading
import types

import torch
import torch.utils._pytree as pytree

try:
    import numpy as np
except ModuleNotFoundError:
    np = None  # type: ignore[assignment]


def is_compiling() -> bool:
    """
    Indicates whether we are tracing/compiling with torch.compile() or torch.export().

    If need to check specifically that TorchDynamo is used, then use
    torch.compiler.is_dynamo_compiling().

    TODO(khabinov): we should deprecate this function and use one of these two:
    * torch.compiler.is_compiling(),
    * torch.compiler.is_dynamo_compiling().
    It will depend on the context where to use what.
    """
    return torch.compiler.is_compiling()


_TLS = threading.local()

_TLS.cached_wrappers = {}


@functools.lru_cache(None)
def create_new_fn(fn):
    from .bytecode_transformation import transform_code_object

    def nothing(*args):
        pass

    new_code = transform_code_object(fn.__code__, nothing)
    new_fn = types.FunctionType(
        new_code,
        fn.__globals__,
        fn.__name__,
        fn.__defaults__,
        fn.__closure__,
    )
    new_fn.__kwdefaults__ = fn.__kwdefaults__
    return new_fn


def wrap_inline(fn):
    """
    Create an extra frame around fn that is not in skipfiles
    """

    # If the code object has already been wrapped before, return the cached
    # wrapper.
    cached_wrappers = _TLS.cached_wrappers

    if isinstance(fn, torch.nn.Module):
        key = fn.forward.__code__
    else:
        key = fn.__code__

    if cached_wrapper := cached_wrappers.get(key):
        return cached_wrapper

    def inner(*args, **kwargs):
        return fn(*args, **kwargs)

    # Create a new function dynamically to avoid Dynamo cache collisions on the
    # same fn.__code__ object.
    # functools.wraps is really important to ensure that __dict__ of the old
    # function is propagated to the new function.
    new_fn = functools.wraps(fn)(create_new_fn(inner))

    cached_wrappers[key] = new_fn

    return new_fn


def call_hook(hook, *args):
    """
    Used by compiled autograd to handle hook returning None
    """
    result = hook(*args)
    if result is None:
        return args[0]
    return result


def wrap_numpy(f):
    r"""Decorator that turns a function from ``np.ndarray``s to ``np.ndarray``s into a function
    from ``torch.Tensor``s to ``torch.Tensor``s.
    """
    if not np:
        return f

    @functools.wraps(f)
    def wrap(*args, **kwargs):
        args, kwargs = pytree.tree_map_only(
            torch.Tensor, lambda x: x.numpy(), (args, kwargs)
        )
        out = f(*args, **kwargs)
        return pytree.tree_map_only(np.ndarray, lambda x: torch.as_tensor(x), out)

    return wrap


class FakeContext:
    def __init__(self, saved_tensors):
        # this will cache the results of saved_tensors
        # and will no longer call into c++ binding
        self.saved_tensors = saved_tensors


def call_backward(backward_fn, saved_tensors, *args):
    grads = backward_fn(FakeContext(saved_tensors), *args)

    # in eager, we wrap in a tuple when there's only one grad output
    if type(grads) is not tuple:
        grads = (grads,)

    return grads


def untyped_storage_size(x: torch.Tensor):
    return x.untyped_storage().size()


def call_hook_from_backward_state(*args, bw_state, hook_name: str, **kwargs):
    return getattr(bw_state, hook_name)(*args, **kwargs)


def call_module_hooks_from_backward_state(
    _, result, *args, bw_state, hooks_name: str, module_name: str
):
    module = getattr(bw_state, module_name)
    hooks = getattr(bw_state, hooks_name)
    for hook in hooks:
        new_result = hook(module, result, *args)
        if new_result is not None:
            result = new_result
    return result
