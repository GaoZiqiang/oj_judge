#!/usr/bin/env python
#coding=utf-8
import os
import re
import sys
import shutil
import subprocess
import codecs
import logging
import shlex
import time
import config
import lorun
import threading
import MySQLdb
from db import run_sql,run_sql_yield
from Queue import Queue
def low_level():
    try:
        os.setuid(int(os.popen("id -u %s"%"nobody").read())) 
    except Exception as e:
        logging.error(e)
try: 
    #降低程序运行权限，防止恶意代码
    os.setuid(int(os.popen("id -u %s"%"nobody").read())) 
except:
    logging.error("please run this program as root!")
    sys.exit(-1)
#初始化队列
q = Queue(config.queue_size)
#数据库锁，保证一个时间只能一个程序都写数据库
#dblock = threading.Lock()
runid_inqueue_set = set()
sql_yield = run_sql_yield()
sql_yield.next()

def worker():
    '''工作线程，循环扫描队列，获得评判任务并执行'''
    while True:
        if q.empty() is True: #队列为空，空闲
            logging.info("%s idle"%(threading.current_thread().name))
        task = q.get()  # 获取任务，如果队列为空则阻塞
        solution_id = task['solution_id']
        problem_id = task['problem_id']
        language = task['pro_lang']
        user_id = task['user_id']
        runid_inqueue_set.remove(int(solution_id))
#        dblock.acquire()
        update_solution_status(solution_id) #将状态改为judging
#        dblock.release()
        data_count = get_data_count(task['problem_id']) #获取测试数据的个数
        logging.info("judging %s"%solution_id)
        result=run(problem_id,solution_id,language,data_count,user_id) #评判
        logging.info("%s result %s"%(result['solution_id'],result['result']))
#        dblock.acquire()
        update_result(result) #将结果写入数据库
#        dblock.release()
        if config.auto_clean == True:  #清理work目录
            clean_work_dir(result['solution_id'])
        q.task_done()   #一个任务完成

def clean_work_dir(solution_id):
    '''清理word目录，删除临时文件'''
    dir_name = os.path.join(config.work_dir,str(solution_id))
    try:
        shutil.rmtree(dir_name)
    except:
        logging.error("rm error")

def start_work_thread():
    '''开启工作线程'''
    for i in range(config.count_thread):
        t = threading.Thread(target=worker)
        t.deamon = True
        t.start()

def start_get_task():
    '''开启获取任务线程'''
    t = threading.Thread(target=put_task_into_queue, name="get_task")
    t.deamon = True
    t.start()

def get_data_count(problem_id):
    '''获得测试数据的个数信息'''
    full_path = os.path.join(config.data_dir,str(problem_id))
    try:
        files = os.listdir(full_path)
    except OSError,e:
        logging.error(e)
        return 0
    except Exception as e:
        logging.error(e)
        return 0
    count = 0
    for item in files:
        if item.endswith(".in") and item.startswith("data"):
            count += 1
    return count

def update_solution_status(solution_id,result=12):
    '''实时更新评测信息'''
    update_sql = "update solution set result = %s where id = %s"%(result,solution_id)
    sql_yield.send(update_sql)
#    run_sql(update_sql)
    return 0

def update_result(result):
    '''更新评测结果'''
    #更新solution信息
    sql = "update solution set take_time = %s , take_memory = %s, result = %s where id = %s"%(result['take_time'],result['take_memory'],result['result'],result['solution_id'])
    #更新用户解题数和做题数信息
    update_ac_sql = "update user_statistics set accepts_count = (select count(distinct problem_id) from solution where result = 1 and user_id = %s) where id = %s;"%(result['user_id'],result['user_id'])
    update_sub_sql = "update user_statistics set solutions_count = (select count(problem_id) from solution where user_id = %s) where id = %s;"%(result['user_id'],result['user_id'])
    #更新题目AC数和提交数信息
    update_problem_ac="UPDATE problem_statistics SET accepts_count=(SELECT count(*) FROM solution WHERE problem_id=%s AND result=1) WHERE id=%s"%(result['problem_id'],result['problem_id'])
    update_problem_sub="UPDATE problem_statistics SET solutions_count=(SELECT count(*) FROM solution WHERE problem_id=%s) WHERE id=%s"%(result['problem_id'],result['problem_id'])
#    run_sql([sql,update_ac_sql,update_sub_sql,update_problem_ac,update_problem_sub])
    sql_yield.send([sql,update_ac_sql,update_sub_sql,update_problem_ac,update_problem_sub])
    return 0

def update_compile_info(solution_id,info):
    '''更新数据库编译错误信息'''
    info = MySQLdb.escape_string(info)
    sql = "insert into compile_info(code_id,content) values (%s,'%s')"%(solution_id,info)
   # run_sql(sql)
    sql_yield.send(sql)
    return 0

def get_problem_limit(problem_id):
    '''获得题目的时间和内存限制'''
    sql = "select time_limit,memory_limit from problem where id = %s"%problem_id
   # data = run_sql(sql)
    data = sql_yield.send(sql)
    return data[0]

def get_code(solution_id,problem_id,pro_lang):
    '''从数据库获取代码并写入work目录下对应的文件'''
    file_name = {
        "gcc":"main.c",
        "g++":"main.cpp",
        "java":"Main.java",
        'ruby':"main.rb",
        "perl":"main.pl",
        "pascal":"main.pas",
        "go":"main.go",
        "lua":"main.lua",
        "dao":"main.dao",
        'python2':'main.py',
        'python3':'main.py',
        "haskell":"main.hs"
    }
    select_code_sql = "select content from code where solution_id = %s"%solution_id
    #feh = run_sql(select_code_sql)
    feh = sql_yield.send(select_code_sql)
    if feh is not None:
        try:
            code = feh[0][0]
        except:
            logging.error("1 cannot get code of runid %s"%solution_id)
            return False
    else:
        logging.error("2 cannot get code of runid %s"%solution_id)
        return False
    try:
        work_path = os.path.join(config.work_dir,str(solution_id))
        low_level()
        os.mkdir(work_path)
    except OSError,e:
        if str(e).find("exist")>0: #文件夹已经存在
            logging.info("dir exist")
        else:
            logging.error(e)
            return False
    except Exception as e:
        logging.error(e)
        return False
    try:
        real_path = os.path.join(config.work_dir,str(solution_id),file_name[pro_lang])
    except KeyError,e:
        logging.error(e)
        return False
    except Exception as e:
        logging.error(e)
        return False
    try:
        low_level()
        f = codecs.open(real_path,'w')
        code = del_code_note(code,pro_lang) #删除注释,防止因为中文问题无法将代码写入文件
        try:
            f.write(code)
        except:
            logging.error("%s not write code to file"%solution_id)
            f.close()
            return False
        f.close()
    except OSError,e:
        logging.error(e)
        return False
    except Exception as e:
        logging.error(e)
        return False
    return True

def del_code_note(code,pro_lang):
    '''删除代码注释'''
    if pro_lang in ['gcc','g++','java','go']:
        code = re.sub("//.*",'',code)
        code = re.sub("/\*.*?\*/",'',code,flags=re.M|re.S)
#    elif pro_lang in ['python2','python3']:
#        code = re.sub("#.*",'',code)
#        code = re.sub(r"'''.*?'''",'',code,flags=re.M|re.S)
#        code = re.sub(r'""".*?"""','',code,flags=re.M|re.S)
    return code

def put_task_into_queue():
    '''循环扫描数据库,将任务添加到队列'''
    while True:
#        q.join() #阻塞程序,直到队列里面的任务全部完成
        sql = "select id,problem_id,user_id,contest_id,program_language from solution where result = 0"
        #data = run_sql(sql)
        data = sql_yield.send(sql)
        time.sleep(0.2) #延时0.2秒,防止因速度太快不能获取代码
        for i in data:
            solution_id,problem_id,user_id,contest_id,pro_lang = i
            if int(solution_id) in runid_inqueue_set:
                time.sleep(0.3)
                continue
    #        dblock.acquire()
            ret = get_code(solution_id,problem_id,pro_lang)
    #        dblock.release()
            if ret == False:
                #防止因速度太快不能获取代码
                time.sleep(1)
     #           dblock.acquire()
                ret = get_code(solution_id,problem_id,pro_lang)
     #           dblock.release()
            if ret == False:
      #          dblock.acquire()
                update_solution_status(solution_id,11)
      #          dblock.release()
                clean_work_dir(solution_id)
                continue
            task = {
                "solution_id":solution_id,
                "problem_id":problem_id,
                "contest_id":contest_id,
                "user_id":user_id,
                "pro_lang":pro_lang,
            }
            runid_inqueue_set.add(int(solution_id))
            q.put(task)
        time.sleep(0.5)

def compile(solution_id,language):
    low_level()
    '''将程序编译成可执行文件'''
    language = language.lower()
    dir_work = os.path.join(config.work_dir,str(solution_id))
#    if language == "ruby":
#        return True
    build_cmd = {
        "gcc"    : "gcc main.c -o main -Wall -lm -O2 -std=c99 --static -DONLINE_JUDGE",
        "g++"    : "g++ main.cpp -O2 -Wall -lm --static -DONLINE_JUDGE -o main",
        "java"   : "javac Main.java",
        "ruby"   : "ruby -c main.rb",
        "perl"   : "perl -c main.pl",
        "pascal" : 'fpc main.pas -O2 -Co -Ct -Ci',
        "go"     : '/opt/golang/bin/go build -ldflags "-s -w"  main.go',
        "lua"    : 'luac -o main main.lua',
		"dao"    : "ls",
        "python2": 'python2 -m py_compile main.py',
        "python3": 'python3 -m py_compile main.py',
        "haskell": "ghc -o main main.hs",
    }
    if language not in build_cmd.keys():
        return False
    p = subprocess.Popen(build_cmd[language],shell=True,cwd=dir_work,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    out,err =  p.communicate()#获取编译错误信息
    err_txt_path = os.path.join(config.work_dir,str(solution_id),'error.txt')
    f = file(err_txt_path,'w')
    f.write(err)
    f.write(out)
    f.close()
    if p.returncode == 0: #返回值为0,编译成功
        return True
 #   dblock.acquire()
    update_compile_info(solution_id,err+out) #编译失败,更新题目的编译错误信息
 #   dblock.release()
    return False

def judge_result(problem_id,solution_id,data_num):
    low_level()
    '''对输出数据进行评测'''
    logging.debug("Judging result")
    currect_result = os.path.join(config.data_dir,str(problem_id),'data%s.out'%data_num)
    user_result = os.path.join(config.work_dir,str(solution_id),'out%s.txt'%data_num)
    try:
        curr = file(currect_result).read().replace('\r','').rstrip()#删除\r,删除行末的空格和换行
        user = file(user_result).read().replace('\r','').rstrip()
    except:
        return False
    if curr == user:       #完全相同:AC
        return "Accepted"
    if curr.split() == user.split(): #除去空格,tab,换行相同:PE
        return "Presentation Error"
    if curr in user:  #输出多了
        return "Output limit"
    return "Wrong Answer"  #其他WA

def judge_one_mem_time(solution_id,problem_id,data_num,time_limit,mem_limit,language):
    rst = None
    low_level()
    '''评测一组数据'''
    input_path = os.path.join(config.data_dir,str(problem_id),'data%s.in'%data_num)
    try:
        input_data = file(input_path)
    except:
        return False
    output_path = os.path.join(config.work_dir,str(solution_id),'out%s.txt'%data_num)
    temp_out_data = file(output_path,'w')
    if language == 'java':
        cmd = 'java -cp %s Main'%(os.path.join(config.work_dir,str(solution_id)))
        main_exe = shlex.split(cmd)
    elif language == 'python2':
        cmd = 'python2 %s'%(os.path.join(config.work_dir,str(solution_id),'main.pyc'))
        main_exe = shlex.split(cmd)
    elif language == 'python3':
        cmd = 'python3 %s'%(os.path.join(config.work_dir,str(solution_id),'__pycache__/main.cpython-33.pyc'))
        main_exe = shlex.split(cmd)
    elif language == 'lua':
        cmd = "lua %s"%(os.path.join(config.work_dir,str(solution_id),"main"))
        main_exe = shlex.split(cmd)
    elif language == "ruby":
        cmd = "ruby %s"%(os.path.join(config.work_dir,str(solution_id),"main.rb"))
        main_exe = shlex.split(cmd)
    elif language == "perl":
        cmd = "perl %s"%(os.path.join(config.work_dir,str(solution_id),"main.pl"))
        main_exe = shlex.split(cmd)
    elif language == "dao":
        cmd = "dao %s"%(os.path.join(config.work_dir,str(solution_id),"main.dao"))
        main_exe = shlex.split(cmd)
    else:
        main_exe = [os.path.join(config.work_dir,str(solution_id),'main'),]
    runcfg = {
        'args':main_exe,
        'fd_in':input_data.fileno(),
        'fd_out':temp_out_data.fileno(),
        'timelimit':time_limit, #in MS
        'memorylimit':mem_limit, #in KB
    }
    low_level()
    try:
        rst = lorun.run(runcfg)
    except:
        logging.error("lorun Error")
    input_data.close()
    temp_out_data.close()
    logging.debug(rst)
    return rst

def check_dangerous_code(solution_id,language):
    if language in ['python2','python3']:
        code = file('/work/%s/main.py'%solution_id).readlines()
        support_modules = [
            're',       #正则表达式
            'sys',      #sys.stdin
            'string',   #字符串处理
            'scanf',    #格式化输入
            'math',     #数学库
            'cmath',    #复数数学库
            'decimal',  #数学库，浮点数
            'numbers',  #抽象基类
            'fractions',#有理数
            'random',   #随机数
            'itertools',#迭代函数
            'functools',#Higher order functions and operations on callable objects
            'operator', #函数操作
            'readline', #读文件
            'json',     #解析json
            'array',    #数组
            'sets',     #集合
            'queue',    #队列
            'types',    #判断类型
        ]
        for line in code:
            if line.find('import') >=0:
                words = line.split()
                tag = 0
                for w in words:
                    if w in support_modules:
                        tag = 1
                        break
                if tag == 0:
                    return False
        return True
    if language in ['gcc','g++']:
        try:
            code = file('/work/%s/main.c'%solution_id).read()
        except:
            code = file('/work/%s/main.cpp'%solution_id).read()
        if code.find('system')>=0:
            return False
        return True
#    if language == 'java':
#        code = file('/work/%s/Main.java'%solution_id).read()
#        if code.find('Runtime.')>=0:
#            return False
#        return True
    if language == 'go':
        code = file('/work/%s/main.go'%solution_id).read()
        danger_package = [
            'os','path','net','sql','syslog','http','mail','rpc','smtp','exec','user',
        ]
        for item in danger_package:
            if code.find('"%s"'%item) >= 0:
                return False
        return True



def judge(solution_id,problem_id,data_count,time_limit,mem_limit,program_info,result_code,language):
    low_level()
    '''评测编译类型语言'''
    max_mem = 0
    max_time = 0
    if language in ["java",'python2','python3','ruby','perl']:
        time_limit = time_limit * 2
        mem_limit = mem_limit * 2
    for i in range(data_count):
        ret = judge_one_mem_time(solution_id,problem_id,i+1,time_limit+10,mem_limit,language)
        if ret == False:
            continue
        if ret['result'] == 5:
            program_info['result'] =result_code["Runtime Error"]
            return program_info
        elif ret['result'] == 2:
            program_info['result'] = result_code["Time Limit Exceeded"]
            program_info['take_time'] = time_limit+10
            return program_info
        elif ret['result'] == 3:
            program_info['result'] =result_code["Memory Limit Exceeded"]
            program_info['take_memory'] = mem_limit
            return program_info
        if max_time < ret["timeused"]:
            max_time = ret['timeused']
        if max_mem < ret['memoryused']:
            max_mem = ret['memoryused']
        result = judge_result(problem_id,solution_id,i+1)
        if result == False:
            continue
        if result == "Wrong Answer" or result == "Output limit":
            program_info['result'] = result_code[result]
            break
        elif result == 'Presentation Error':
            program_info['result'] = result_code[result]
        elif result == 'Accepted':
            if program_info['result'] != 'Presentation Error':
                program_info['result'] = result_code[result]
        else:
            logging.error("judge did not get result")
    program_info['take_time'] = max_time
    program_info['take_memory'] = max_mem
    return program_info

def run(problem_id,solution_id,language,data_count,user_id):
    low_level()
    '''获取程序执行时间和内存'''
#    dblock.acquire()
    time_limit,mem_limit=get_problem_limit(problem_id)
#    dblock.release()
    program_info = {
        "solution_id":solution_id,
        "problem_id":problem_id,
        "take_time":0,
        "take_memory":0,
        "user_id":user_id,
        "result":0,
    }
    result_code = {
        "Waiting":0,
        "Accepted":1,
        "Time Limit Exceeded":2,
        "Memory Limit Exceeded":3,
        "Wrong Answer":4,
        "Runtime Error":5,
        "Output limit":6,
        "Compile Error":7,
        "Presentation Error":8,
        "System Error":11,
        "Judging":12,
    }
#    if check_dangerous_code(solution_id,language) == False:
#        program_info['result'] = result_code["Runtime Error"]
#        return program_info
    compile_result = compile(solution_id,language)
    if compile_result is False:#编译错误
        program_info['result'] = result_code["Compile Error"]
        return program_info
    if data_count == 0:#没有测试数据
        program_info['result'] = result_code["System Error"]
        return program_info
    result = judge(solution_id,problem_id,data_count,time_limit,mem_limit,program_info,result_code,language)
    logging.debug(result)
    return result

def check_thread():
    low_level()
    '''检测评测程序是否存在,小于config规定数目则启动新的'''
    while True:
        try:
            if threading.active_count() < config.count_thread + 2:
                logging.info("start new thread")
                t = threading.Thread(target=worker)
                t.deamon = True
                t.start()
            time.sleep(1)
        except Exception as e:
            logging.error(e)

def start_protect():
    '''开启守护进程'''
    low_level()
    t = threading.Thread(target=check_thread, name="check_thread")
    t.deamon = True
    t.start()

def main():
    low_level()
    logging.basicConfig(level=logging.INFO,
                        format = '%(asctime)s --- %(message)s',)
    start_get_task()
    start_work_thread()
    start_protect()

if __name__=='__main__':
    main()
