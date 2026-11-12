# 初使化记录

每个里程碑结束后追加：实测确认的 Moomoo API 行为（注明文档章节或探针脚本）和踩过的坑。

## PostgresSQL @17

### Claude output
下面的命令都在**你自己的终端**里运行，不要让 Claude 跑：其中要输入 `mooapp_real` 的密码，这个密码属于 secrets。路径和版本行为是按 Homebrew 的一般做法写的，我在这里没法实际核实，每一步跑完请看一眼输出。

## 1. 安装 17，停掉 14

```bash
brew services list                      # 看 postgresql@14 是否在运行
brew services stop postgresql@14        # 如果在运行
brew install postgresql@17
```

`postgresql@17` 是 keg-only 包，不会自动加进 PATH，需要手动加：

```bash
echo 'export PATH="/opt/homebrew/opt/postgresql@17/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
psql --version                          # 应显示 17.x
```

**14 里的旧数据：** 本项目还没有往 14 写过任何数据。如果 14 里也没有你自己的数据，就不需要迁移，直接用 17 的新数据目录。如果 14 里有你要保留的数据，在停掉 14 之前先用 `pg_dumpall` 导出，再导入到 17。`pg_upgrade` 也能迁移，但步骤更多。

## 2. 改配置：只监听本机，所有连接都要密码

Homebrew 安装时会自动初始化数据目录，一般在 `/opt/homebrew/var/postgresql@17`。如果这个目录不存在，先运行：

```bash
initdb -D /opt/homebrew/var/postgresql@17 --auth-local=scram-sha-256 --auth-host=scram-sha-256 -U "$USER" -W
```

`-W` 会提示你给超级用户设密码。

然后编辑 `/opt/homebrew/var/postgresql@17/postgresql.conf`，确认这三行：

```
listen_addresses = '127.0.0.1'
port = 5432
password_encryption = scram-sha-256
```

再编辑同目录下的 `pg_hba.conf`，把原有的规则行全部替换成下面这些。Homebrew 默认生成的规则可能是 `trust`，必须改掉：

```
# TYPE  DATABASE      USER          ADDRESS        METHOD
local   all           all                          scram-sha-256
host    mooapp        mooapp_sim    127.0.0.1/32   scram-sha-256
host    mooapp        mooapp_real   127.0.0.1/32   scram-sha-256
host    mooapp_test   mooapp_sim    127.0.0.1/32   scram-sha-256
host    all           all           127.0.0.1/32   scram-sha-256
```

注意：如果你的超级用户之前是靠 `trust` 登录、还没有设过密码，那么改成 `scram-sha-256` 之后，你自己也会登录不上。处理办法是：先保留 `local all all trust` 这一行，启动后用 `\password` 给自己设好密码，再把它改成 `scram-sha-256`，最后执行 `brew services restart postgresql@17`。

## 3. 启动，并设为开机自启

```bash
brew services start postgresql@17
pg_isready -h 127.0.0.1 -p 5432         # 应显示 accepting connections
```

## 4. 建角色、库、schema，并授权

用超级用户登录：

```bash
psql -d postgres
```

在 psql 里执行下面的 SQL。密码用 `\password` 交互输入，不会留在 shell 历史里：

```sql
-- 角色
CREATE ROLE mooapp_sim  LOGIN;
CREATE ROLE mooapp_real LOGIN;
\password mooapp_sim
\password mooapp_real

-- 库（属主是你的超级用户）
CREATE DATABASE mooapp;
CREATE DATABASE mooapp_test;

-- 只允许指定角色连接
REVOKE ALL ON DATABASE mooapp      FROM PUBLIC;
REVOKE ALL ON DATABASE mooapp_test FROM PUBLIC;
GRANT CONNECT ON DATABASE mooapp      TO mooapp_sim, mooapp_real;
GRANT CONNECT, CREATE ON DATABASE mooapp_test TO mooapp_sim;   -- 测试每次自己建临时 schema

-- mooapp 里的两个 schema，属主分别是对应角色
\c mooapp
REVOKE ALL ON SCHEMA public FROM PUBLIC;
CREATE SCHEMA simulate AUTHORIZATION mooapp_sim;
CREATE SCHEMA real     AUTHORIZATION mooapp_real;
REVOKE ALL ON SCHEMA simulate FROM PUBLIC;
REVOKE ALL ON SCHEMA real     FROM PUBLIC;
ALTER ROLE mooapp_sim  IN DATABASE mooapp SET search_path = simulate;
ALTER ROLE mooapp_real IN DATABASE mooapp SET search_path = real;

-- 测试库
\c mooapp_test
REVOKE ALL ON SCHEMA public FROM PUBLIC;
\q
```

`mooapp_sim` 和 `mooapp_real` 互相没有对方 schema 的 `USAGE` 权限，这正是 SPEC 要求的隔离。表、索引、视图、存储过程以后由 `db/` 下的初始化代码建，这一步只建空的 schema。

## 5. 验证隔离

```bash
psql -h 127.0.0.1 -U mooapp_sim -d mooapp -c "SELECT current_schema();"   # 应为 simulate
psql -h 127.0.0.1 -U mooapp_sim -d mooapp -c "CREATE TABLE real.t(x int);" # 应报 permission denied
```

## 6. 把密码放到约定的位置

- **`mooapp_sim` 的密码**：先 `cp config/db.local.example.toml config/db.local.toml`，再把密码填进去。这个文件已被 gitignore。
- **`mooapp_real` 的密码**：手动填到 `~/.config/mooapp/secrets.toml` 的 `[postgres] real_password`，然后 `chmod 600` 这个文件。

做完后，可以在主对话里让 Claude 更新 PROGRESS，并在 M1 里实现 `doctor` 的端口和隔离检查。

### Shell log

ben@Bins-Mac-mini Downloads % brew install postgresql@17
... 
...
Success. You can now start the database server using:

    '/opt/homebrew/Cellar/postgresql@17/17.11_1/bin/pg_ctl' -D '/opt/homebrew/var/postgresql@17' -l logfile start

initdb: warning: enabling "trust" authentication for local connections
initdb: hint: You can change this by editing pg_hba.conf or using the option -A, or --auth-local and --auth-host, the next time you run initdb.
🍺  /opt/homebrew/Cellar/postgresql@17/17.11_1: 3,826 files, 75.2MB
==> Caveats
==> postgresql@17
This formula has created a default database cluster with:
  initdb --locale=en_US.UTF-8 -E UTF-8 /opt/homebrew/var/postgresql@17

When uninstalling, some dead symlinks are left behind so you may want to run:
  brew cleanup --prune-prefix

postgresql@17 is keg-only, which means it was not symlinked into /opt/homebrew,
because this is an alternate version of another formula.

If you need to have postgresql@17 first in your PATH, run:
  printf '%s\n' export\ PATH\=/opt/homebrew/opt/postgresql@17/bin:\$PATH >> ~/.zshrc

For compilers to find postgresql@17 you may need to set:
  export LDFLAGS=-L/opt/homebrew/opt/postgresql@17/lib
  export CPPFLAGS=-I/opt/homebrew/opt/postgresql@17/include

The following postgresql@17 executables are shadowed by linked postgresql@14 commands in /opt/homebrew/bin:
clusterdb           ecpg                pg_basebackup       pg_dump             pg_resetwal         pg_upgrade          psql
createdb            initdb              pg_checksums        pg_dumpall          pg_restore          pg_verifybackup     reindexdb
createuser          oid2name            pg_config           pg_isready          pg_rewind           pg_waldump          vacuumdb
dropdb              pg_amcheck          pg_controldata      pg_receivewal       pg_test_fsync       pgbench             vacuumlo
dropuser            pg_archivecleanup   pg_ctl              pg_recvlogical      pg_test_timing      postgres
Running these by name will not invoke the version provided by this formula.
Run `brew link postgresql@17` to switch the active version to this keg.
Disable this behaviour by setting `HOMEBREW_NO_PATH_SHADOW_CHECK=1`.
Hide these hints with `HOMEBREW_NO_ENV_HINTS=1` (see `man brew`).

To start postgresql@17 now and restart at login:
  brew services start postgresql@17
Or, if you don't want/need a background service you can just run:
  LC_ALL="en_US.UTF-8" /opt/homebrew/opt/postgresql@17/bin/postgres -D /opt/homebrew/var/postgresql@17
ben@Bins-Mac-mini Downloads % printf '%s\n' export\ PATH\=/opt/homebrew/opt/postgresql@17/bin:\$PATH >> ~/.zshrc
ben@Bins-Mac-mini Downloads % . ~/.zshrc
ben@Bins-Mac-mini Downloads % psql --version
psql (PostgreSQL) 17.11 (Homebrew)
ben@Bins-Mac-mini Downloads % 
ben@Bins-Mac-mini Downloads % vi /opt/homebrew/var/postgresql@17/postgresql.conf 
ben@Bins-Mac-mini Downloads % -- 修改了  listen_addresses = 'localhost'  ；password_encryption = scram-sha-256  


mooapp=#grant create on database mooapp_test to mooapp_sim;
mooapp=# grant connect on database mooapp_test to mooapp_sim;

mooapp=# \c
You are now connected to database "mooapp" as user "ben".
mooapp=# alter schema simulate owner to mooapp_sim;
ALTER SCHEMA
mooapp=# alter schema real owner to mooapp_real;
ALTER SCHEMA


