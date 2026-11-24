#!/usr/bin/env python3
"""Remove owned legacy reply hooks without changing unrelated hooks or trust."""
import argparse
import json
import os
from pathlib import Path
import secrets
import stat
import sys

import watch_reply_hook as hook

MARK = 'watch-peer-reply-hook-v1'
LIMIT = 1024 * 1024
NONE_ON_WINDOWS = 'No reply hooks to remove: automatic reply notices were never available on Windows.'


def hook_folders_available():
    """Removal opens each folder by handle, refusing links on the way (dir_fd); Windows has no such walk yet. Reply
    notices were retired before they could be installed there, so a Windows profile has none to remove."""
    return os.name == 'posix'


def parent(path, create=False):
    path=os.path.abspath(os.fspath(path));fd=os.open(os.sep,os.O_RDONLY|os.O_DIRECTORY)
    try:
        for part in path.split(os.sep)[1:-1]:
            try:child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            except FileNotFoundError:
                if not create:raise
                os.mkdir(part,0o700,dir_fd=fd)
                child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            os.close(fd);fd=child
        hook.require(os.fstat(fd).st_uid==os.getuid())
        return fd,os.path.basename(path)
    except BaseException:os.close(fd);raise


def snapshot(path):
    fd,name=parent(path)
    try:
        try:file=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
        except FileNotFoundError:return None,0o600
        try:
            meta=os.fstat(file)
            hook.require(stat.S_ISREG(meta.st_mode) and meta.st_uid==os.getuid() and meta.st_nlink==1 and meta.st_size<=LIMIT)
            raw=os.read(file,LIMIT+1);hook.require(len(raw)<=LIMIT)
            return raw,stat.S_IMODE(meta.st_mode)
        finally:os.close(file)
    finally:os.close(fd)


def replace(path,raw,mode,expected):
    fd,name=parent(path,True);temporary='.watch-reply-'+secrets.token_hex(12)
    try:
        hook.require(snapshot(path)[0]==expected)
        out=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,mode,dir_fd=fd)
        try:
            os.fchmod(out,mode);view=memoryview(raw)
            while view:written=os.write(out,view);hook.require(written>0);view=view[written:]
            os.fsync(out)
        finally:os.close(out)
        hook.require(snapshot(path)[0]==expected)
        os.replace(temporary,name,src_dir_fd=fd,dst_dir_fd=fd);os.fsync(fd)
    finally:
        try:os.unlink(temporary,dir_fd=fd)
        except FileNotFoundError:pass
        os.close(fd)


def restore(path,before,mode,expected):
    if before is not None:
        return replace(path,before,mode,expected)
    fd,name=parent(path)
    try:
        hook.require(snapshot(path)[0]==expected)
        os.unlink(name,dir_fd=fd);os.fsync(fd)
    finally:os.close(fd)


def private_directory(path):
    fd,name=parent(Path(path)/'placeholder',True)
    try:
        meta=os.fstat(fd);hook.require(meta.st_uid==os.getuid() and stat.S_IMODE(meta.st_mode)==0o700)
    finally:os.close(fd)


def encoded(value):return (json.dumps(value,indent=2,ensure_ascii=True)+'\n').encode()


def remove_owned(value,owned):
    hooks=value.get('hooks',{});hook.require(type(hooks) is dict)
    groups=hooks.get('PostToolUse',[]);hook.require(type(groups) is list)
    result=[]
    for group in groups:
        hook.require(type(group) is dict and type(group.get('hooks')) is list)
        if group.get('matcher')==owned['matcher']:
            retained=[entry for entry in group['hooks'] if entry!=owned['hooks'][0]]
            if len(retained)!=len(group['hooks']):
                if retained:result.append(dict(group,hooks=retained))
                continue
        result.append(group)
    if result:hooks['PostToolUse']=result
    else:hooks.pop('PostToolUse',None)
    if not hooks:value.pop('hooks',None)
    return value


def configure(provider,path,store,credential_file,accepted=False,remove=False,journal=None):
    if not remove:
        raise ValueError('Automatic reply notices have been removed; only --remove is supported.')
    hook.require(provider in hook.TOOLS)
    def write(path,raw,mode,expected):
        replace(path,raw,mode,expected)
        if journal is not None:journal[str(path)]=raw
    store=Path(store);private_directory(store)
    fd,_=parent(path,True);os.close(fd)
    manifest=store/('install-'+provider+'.json');previous,_=snapshot(manifest)
    old=json.loads(previous) if previous else None
    before,mode=snapshot(path);value=json.loads(before,object_pairs_hook=hook.pairs) if before else {}
    hook.require(type(value) is dict)
    if old:
        hook.require(old['version']==1 and old['provider']==provider and old['path']==str(path))
        value=remove_owned(value,old['owned'])
    if remove:
        if not old:return False
        after=encoded(value)
        write(path,after,mode,before)
        # Retain the private manifest/backup as rollback evidence, marked disabled.
        write(manifest,encoded(dict(old,enabled=False)),0o600,previous)
        return True


def main():
    parser=argparse.ArgumentParser(description='Remove legacy Motet reply-notice hooks. Automatic reply notices are no longer supported.')
    parser.add_argument('--provider',choices=('codex','claude','both'),required=True)
    parser.add_argument('--credential-file');parser.add_argument('--setup',action='store_true')
    parser.add_argument('--remove',action='store_true');parser.add_argument('--revoke',action='store_true',help='Offline global reply-consent revocation; use with --remove while Motet is stopped');parser.add_argument('--accept-native-parent-return',action='store_true')
    parser.add_argument('--port',type=int,default=8793)
    args=parser.parse_args()
    if not args.remove or args.setup or args.credential_file:
        raise ValueError('Automatic reply notices have been removed; use --remove, optionally with --revoke.')
    if not hook_folders_available():
        # Nothing was ever installed here. Revocation touches only Motet's database, under Motet's own lifetime lock.
        if args.revoke:
            sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
            import peer_reply_delivery
            peer_reply_delivery.revoke_offline(str(Path.home()/'.live-room'))
        print(NONE_ON_WINDOWS+(' Saved reply-notice access was turned off.' if args.revoke else ''))
        return
    hook.require(args.remove or args.accept_native_parent_return)
    home=Path.home();state=home/'.live-room';store=state/'reply-hook';private_directory(state);private_directory(store)
    providers=['codex','claude'] if args.provider=='both' else [args.provider]
    paths={'codex':home/'.codex/hooks.json','claude':home/'.claude/settings.json'}
    def install(credential_file):
        tracked=[paths[p] for p in providers]+[store/('install-'+p+'.json') for p in providers]+[store/'watch_reply_hook.py']
        for path in paths.values():
            if path in tracked:fd,_=parent(path,True);os.close(fd)
        originals={str(path):snapshot(path) for path in tracked}
        written={}
        def rollback():
            # Also returned to offline provisioning for a failure after this callback completes.
            # Compare against our exact writes; never overwrite a concurrent owner's changes.
            for path,expected in reversed(list(written.items())):
                if path in originals:
                    before,mode=originals[path]
                    if before!=expected and snapshot(path)[0]==expected:restore(path,before,mode,expected)
        try:
            for provider in providers:
                configure(provider,paths[provider],store,credential_file,args.accept_native_parent_return,args.remove,journal=written)
        except BaseException:
            rollback()
            raise
        return rollback
    if args.revoke:
        hook.require(args.remove and not args.setup and not args.credential_file)
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
        import peer_reply_delivery
        peer_reply_delivery.revoke_offline(str(state),install=lambda:install(''))
    else:
        hook.require(args.remove or args.credential_file)
        install(str(Path(args.credential_file).absolute()) if args.credential_file else '')
    print('Removed owned reply hooks.')


if __name__=='__main__':
    try:main()
    except Exception:raise SystemExit('Reply notices are retired. Only --remove (optionally --revoke while Motet is stopped) is supported. If removal failed, preserve the private backup and review configuration.')
