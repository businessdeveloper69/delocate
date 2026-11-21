"""Exercise a real two-input delocate merge on the selected macOS toolchain."""
import base64
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
from email.parser import BytesParser
from urllib.request import urlopen
from zipfile import ZipFile

BASE = '9aeae5734ea038a0147c0f0d34b917db6fce2a53'
CANDIDATE = '39e34264a35774bd59114cdd9611744911bff896'
ROOT = Path(os.environ['RUNNER_TEMP']) / 'real-wheel-check'
RECEIPTS = ROOT / 'receipts'
SMOKE = '''from zope.interface import Interface, implementer
from zope.interface import _zope_interface_coptimizations as native
import json, platform
class IProbe(Interface): pass
@implementer(IProbe)
class Probe: pass
assert IProbe.providedBy(Probe())
assert native.__file__.endswith('.so')
print(json.dumps({'machine':platform.machine(),'extension':native.__file__.split('/')[-1],'provided_by':True}))'''


def run(args, *, check=True, env=None):
    result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, env=env)
    if check and result.returncode:
        raise RuntimeError(f'{args}: {result.stdout}\n{result.stderr}')
    return result


def info(args, **kwargs):
    return run(args, **kwargs).stdout.strip()


def validate_wheel(wheel, dest, env):
    dest.mkdir(parents=True)
    with ZipFile(wheel) as archive:
        assert archive.testzip() is None
        files = {n for n in archive.namelist() if not n.endswith('/')}
        record, = [n for n in files if n.endswith('.dist-info/RECORD')]
        wheelmeta, = [n for n in files if n.endswith('.dist-info/WHEEL')]
        ext, = [n for n in files if n.endswith('.so')]
        rec = list(csv.reader(io.StringIO(archive.read(record).decode())))
        assert {row[0] for row in rec} == files and len(rec) == len(files)
        for name, digest, size in rec:
            if name == record:
                assert not digest and not size
                continue
            body = archive.read(name)
            encoded = base64.urlsafe_b64encode(hashlib.sha256(body).digest()).rstrip(b'=').decode()
            assert digest == 'sha256=' + encoded and int(size) == len(body), name
        tags = BytesParser().parsebytes(archive.read(wheelmeta)).get_all('Tag')
        assert any('universal2' in t for t in tags) and 'universal2' in wheel.name, (tags,wheel)
        binary = dest / Path(ext).name
        binary.write_bytes(archive.read(ext))
    archs = sorted(info(['lipo','-archs',binary],env=env).split())
    assert archs == ['arm64','x86_64'], archs
    venv = dest / 'env'
    run([sys.executable,'-m','venv',venv])
    py = venv / 'bin/python'
    run([py,'-m','pip','install','--no-index','--no-deps',wheel])
    smoke = json.loads(info([py,'-I','-c',SMOKE],env=env))
    assert smoke['machine'] == platform.machine()
    return dict(wheel=wheel.name, sha256=hashlib.sha256(wheel.read_bytes()).hexdigest(), archs=archs,
                tags=tags, record_files=len(rec), native_import=smoke)


def main():
    ROOT.mkdir(exist_ok=True)
    RECEIPTS.mkdir(exist_ok=True)
    assert sys.version_info[:2] == (3,14)
    if len(sys.argv) == 3 and sys.argv[1]=='cross':
        wheel, = Path(sys.argv[2]).glob('*.whl')
        result=validate_wheel(wheel,ROOT/'cross-import',os.environ.copy())
        print('CROSS_IMPORT',json.dumps(result),flush=True)
        (RECEIPTS/'cross.json').write_text(json.dumps(result,indent=2))
        return
    runner = sys.argv[1]
    tools = {'xcode-27':['27.0','27.1','27.2'], 'macos-26':['26.0.1','26.6'], 'macos-26-intel':['26.0.1','26.6']}[runner]
    workspace=Path(os.environ['GITHUB_WORKSPACE'])
    inputs=ROOT/'inputs';inputs.mkdir()
    wheels=[]; raw_records=[]
    manifest=json.loads((Path(__file__).parent/'inputs.json').read_text())
    for name,url,sha in manifest:
        wheel=inputs/name; wheel.write_bytes(urlopen(url,timeout=45).read())
        assert hashlib.sha256(wheel.read_bytes()).hexdigest()==sha
        wheels.append(wheel)
        with ZipFile(wheel) as archive:
            member,=[n for n in archive.namelist() if n.endswith('.dist-info/RECORD')]
            record=ROOT/(name+'.RECORD');record.write_bytes(archive.read(member));raw_records.append(record)
    assert raw_records[0].read_bytes()!=raw_records[1].read_bytes()
    versions={}
    for name,requirement,pin in [('release','delocate==0.13.0',None),('base',workspace/'upstream',BASE),('candidate',workspace/'candidate',CANDIDATE)]:
        if pin: assert info(['git','-C',requirement,'rev-parse','HEAD']) == pin
        envdir=ROOT/name;run([sys.executable,'-m','venv',envdir]);py=envdir/'bin/python'
        run([py,'-m','pip','install','--disable-pip-version-check',str(requirement)])
        versions[name]=info([py,'-c',"from importlib.metadata import version;print(version('delocate'))"])
    doc=dict(runner=runner,os=info(['sw_vers']),machine=platform.machine(),python=platform.python_version(),versions=versions,inputs=manifest,base=BASE,candidate=CANDIDATE,experiments=[])
    failures=[]
    paths = {Path('/Applications')/f'Xcode_{v}.app':v for v in tools}
    if not any(p.exists() for p in paths):
        paths={Path('/Applications/Xcode.app'):'default'}
    for app,tool in paths.items():
        if not app.exists(): continue
        env=os.environ.copy();env['DEVELOPER_DIR']=str(app/'Contents/Developer')
        xcode=info(['xcodebuild','-version'],env=env)
        xcrun=info(['xcrun','--find','lipo'],env=env)
        case={'selected':tool,'developer_dir':env['DEVELOPER_DIR'],'xcode':xcode,'resolved_lipo':xcrun,'path_lipo':shutil.which('lipo'),'merges':{}}
        doc['experiments'].append(case)
        raw=run(['lipo','-create',*raw_records,'-output',ROOT/'raw-output'],check=False,env=env)
        assert raw.returncode!=0
        case['record_lipo_exit']=raw.returncode
        case['record_lipo_stderr']=raw.stderr
        explicit=run([xcrun,'-create',*raw_records,'-output',ROOT/'raw-explicit-output'],check=False,env=env)
        case['resolved_lipo_stderr']=explicit.stderr
        regression='no eligible inputs found' in raw.stderr
        for label in versions:
            work=ROOT/f'{tool}-{label}';result=run([ROOT/label/'bin/delocate-merge','-w',work,*wheels],check=False,env=env)
            case['merges'][label]={'exit':result.returncode,'stderr':result.stderr[-5200:]}
            print('MERGE',runner,xcode.replace('\n',' '),label,result.returncode, '\n'.join(result.stderr.splitlines()[-3:]),flush=True)
            if label=='candidate' or not regression:
                if result.returncode:
                    failures.append(f'{tool} {label} returned {result.returncode}');continue
                wheel,=work.glob('*.whl')
                try: check=validate_wheel(wheel,ROOT/f'{tool}-{label}-check',env)
                except Exception as err:
                    failures.append(f'{tool} {label} validation: {err}');continue
                case['merges'][label]['validated']=check
                print('WHEEL',label,json.dumps(check),flush=True)
                if label=='candidate' and runner=='xcode-27':
                    export=ROOT/'cross-wheel';export.mkdir(exist_ok=True)
                    shutil.copyfile(wheel,export/wheel.name)
            elif result.returncode and 'no eligible inputs found' in result.stderr and '.dist-info/RECORD' in result.stderr:
                case['merges'][label]['issue_279_reproduced']=True
            else:
                failures.append(f'{tool} {label}: exact issue signature missing')
        (RECEIPTS/f'{runner}.json').write_text(json.dumps(doc,indent=2)+'\n')
    assert doc['experiments'] and not failures, failures

if __name__=='__main__':
    main()
