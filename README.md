# MySQL 配置巡检工具（gdb_cfg_checker）

检查 MySQL 数据库运行配置与配置文件配置的工具。支持多主机 `my.cnf` 两两差异对比、文件配置与运行参数对比（按路径提取 IP 或显式指定 host），并附带最佳实践/加固巡检。

## 功能特性

> **三种模式总览**（模式间相互隔离，参数各司其职）
>
> | 模式 | 目标 host 来源 | 配置来源 | 典型场景 |
> |------|---------------|----------|----------|
> | `file` | 不连接 DB | 目录扫描，多文件两两对比 | 多主机配置一致性巡检 |
> | `file-vs-running` | 从文件路径提取 IP | 目录扫描 | 多主机文件 vs 运行参数 |
> | `file-vs-running-host` | 显式 `--mysql-host` | 单个 `--config-file` | 单实例 / 路径不含 IP（如本地） |

### 1. 配置文件差异对比（file 模式）
- 自动递归扫描多个主机的 `my.cnf`（或自定义文件名）配置文件
- 检测同一文件内的重复 key 并警告（含 `!include` 合并产生的重复）
- 支持按 section 过滤对比（`--section`）
- 支持只显示有差异的主机对（`--diff-only`）
- 比较前对值做类型归一化，消除 `1G` vs `1073741824`、`ON` vs `on` 的假阳性

### 2. 文件配置 vs 运行参数对比（file-vs-running 模式）
- 通过 **PyMySQL** 连接远程 MySQL，获取 `SHOW GLOBAL VARIABLES`
- 自动从文件路径提取 IP 并连接对应实例
- 按参数类型（布尔/容量/整数/字符串/路径）正确归一化后比较
- 支持 `--workers` 并发连接、SSL/TLS（`--ssl`）、连接重试（`--retries`）
- 支持 `--best-practice` 对运行参数做加固巡检

### 3. 显式指定 host：文件 vs 单实例（file-vs-running-host 模式）
- 用 `--config-file` 指定**单个**配置文件，`--mysql-host` / `--mysql-port` 显式指定目标实例
- 不依赖路径提取 IP，适合本地实例或路径不含 IP 的场景（如 `./my.cnf` 对 `127.0.0.1`）
- 复用与 `file-vs-running` 相同的归一化、对比、加固巡检逻辑
- 只对比这一个文件 vs 这一个实例，**不做**多主机两两对比
- 同样支持 `--best-practice` / `--allow-diff` / `--json` / `--strict`

### 4. my.cnf 解析增强
- 选项名连字符/下划线等价（`log-bin` ≡ `log_bin`）
- 支持无值布尔选项（`skip-name-resolve`、`log-bin` 等，视为 ON）
- 剥离行尾 `# ...` / `; ...` 注释
- 递归解析 `!include` / `!includedir`（带环路检测、相对路径基于包含文件目录）

### 5. 安全凭证管理
- 支持 `--password-conf` 指定独立凭证文件，从 `[mysql]` 节读取（兼容 `user`/`username` 两种键名）
- 支持 `--mysql-config` 指定 MySQL 配置文件（读 `[client]` 节）
- 支持 `MYSQL_USER` / `MYSQL_PASSWORD` 环境变量
- 密码**只从文件或环境变量读取，不接受命令行参数**，避免泄露进进程列表/日志

凭证优先级（从高到低）：
1. `--password-conf`（`[mysql]` 节）
2. `--mysql-config`（`[client]` 节）
3. `~/.my.cnf` → `/etc/my.cnf`
4. `MYSQL_USER` / `MYSQL_PASSWORD` 环境变量

#### `--password-conf` 文件格式

```ini
[mysql]
username=root
password=your_secret
port=3306        # 可选
socket=/tmp/mysql.sock   # 可选
```

> 同一文件内若同时存在 `[client]` 和 `[mysql]` 节，`[mysql]` 节的值优先。

### 6. 可配置路径与文件名
- `--config-path` 指定路径模板（`{ip}` 占位符）
- `--config-name` 指定要匹配的配置文件名（可重复，默认 `my.cnf` / `mysqld.cnf` / `mysql.cnf`）

### 7. 期望差异白名单
- `--allow-diff` 指定允许不同的参数（如 `server_id,port,pid-file,socket`）

### 8. 结构化输出与 CI 集成
- `--json` 输出结构化结果，便于接 CI / 监控
- `--strict` 发现差异或连接失败时以非零码退出（0=无差异，2=有差异/连接失败，1=参数错误）

## 安装

```bash
pip3 install -r requirements.txt   # 安装 PyMySQL
```

## 使用方法

### 基本用法

```bash
# 配置文件两两对比
python3 compare_my_cnf.py <base_directory> --mode file

# 按 section 过滤
python3 compare_my_cnf.py <base_directory> --section mysqld

# 只显示有差异的主机对
python3 compare_my_cnf.py <base_directory> --diff-only
```

### 文件 vs 运行参数

```bash
# 自动从路径提取 IP 连接对应实例
python3 compare_my_cnf.py <base_directory> \
    --mode file-vs-running --mysql-config ~/.my.cnf

# 用独立凭证文件（[mysql] 节，密码不进命令行）
python3 compare_my_cnf.py <base_directory> \
    --mode file-vs-running --password-conf /etc/my_tool/creds.cnf

# 使用环境变量凭证
MYSQL_USER=root MYSQL_PASSWORD=secret python3 compare_my_cnf.py <base_directory> \
    --mode file-vs-running

# 并发 + SSL + 加固巡检
python3 compare_my_cnf.py <base_directory> \
    --mode file-vs-running --mysql-config ~/.my.cnf \
    --workers 8 --ssl --best-practice

# 允许特定参数差异
python3 compare_my_cnf.py <base_directory> \
    --mode file-vs-running --mysql-config ~/.my.cnf \
    --allow-diff "server_id,port,pid-file,socket"
```

### 显式指定 host（单实例 / 本地）

```bash
# 路径不含 IP、或对比本地实例时用此模式
python3 compare_my_cnf.py \
    --mode file-vs-running-host \
    --config-file ./my.cnf \
    --mysql-host 127.0.0.1 --mysql-port 3306 \
    --password-conf passwd.cnf --best-practice

# JSON 输出 + 严格退出码（有差异则非零退出）
python3 compare_my_cnf.py \
    --mode file-vs-running-host \
    --config-file ./my.cnf --mysql-host 127.0.0.1 \
    --password-conf passwd.cnf --json --strict
```

### CI 集成

```bash
# JSON 输出 + 严格退出码：有差异则非零退出
python3 compare_my_cnf.py <base_directory> \
    --mode file-vs-running --mysql-config ~/.my.cnf \
    --json --strict
```

> **关键设计**：`file-vs-running` 模式无需 `--mysql-host`——工具从配置文件路径提取 IP（如
> `/data/goldendb/10.0.1.1/...` 中的 `10.0.1.1`），用该 IP 连接对应实例。
> 若路径不含 IP 或要对比本地实例，改用 `file-vs-running-host` 模式显式指定。

## 命令行参数

| 参数 | 说明 | 默认 |
|------|------|------|
| `<base_directory>` | 配置文件基础目录（`file`/`file-vs-running` 必填；`file-vs-running-host` 不用） | — |
| `--mode` | 对比模式：`file` / `file-vs-running` / `file-vs-running-host` | `file` |
| `--section` | 只对比指定 section | 全部 |
| `--diff-only` | 只显示有差异的主机对 | 关 |
| `--config-path` | 路径模板（含 `{ip}` 占位符） | 递归搜索 |
| `--config-name` | 要匹配的文件名（可重复） | `my.cnf` 等 |
| `--config-file` | 单个配置文件路径（仅 `file-vs-running-host` 必填） | - |
| `--mysql-port` | MySQL 端口 | 3306 |
| `--mysql-host` | 目标实例主机（仅 `file-vs-running-host` 必填；其他模式忽略并警告） | - |
| `--mysql-config` | MySQL 配置文件（含 `[client]` 节） | — |
| `--password-conf` | 凭证文件（`[mysql]` 节，`password=`/`username=`） | — |
| `--allow-diff` | 允许不同的参数列表（逗号分隔） | — |
| `--workers` | 并发连接数 | 1 |
| `--ssl` | 启用 SSL 连接 | 关 |
| `--ssl-ca` / `--ssl-cert` / `--ssl-key` | SSL 证书路径 | — |
| `--retries` | 连接失败重试次数 | 0 |
| `--best-practice` | 对运行参数做加固巡检 | 关 |
| `--json` | JSON 输出 | 关 |
| `--strict` | 有差异/连接失败时非零退出 | 关 |

## 退出码

| 码 | 含义 |
|----|------|
| 0 | 成功，无差异 |
| 1 | 参数错误 / 目录不存在 / 未找到配置文件 / `--config-file` 非文件 / 缺凭证 |
| 2 | `--strict` 下发现差异或连接失败 |

## 输出符号

- `✓` 一致　`⚠` 差异　`✗` 连接失败
- `~` 值不同　`+` 仅一侧存在　`-` 仅一侧缺失

## 最佳实践巡检项（`--best-practice`）

| 参数 | 检查 |
|------|------|
| `innodb_flush_log_at_trx_commit` | 建议 = 1（崩溃不丢已提交事务） |
| `sync_binlog` | 建议非 0，避免主机崩溃丢 binlog |
| `binlog_format` | 复制场景建议 ROW |
| `log_bin` | 建议开启（时间点恢复/复制） |
| `gtid_mode` | 建议走向 ON |
| `skip_name_resolve` | 建议开启（避免 DNS 抖动/连接卡住） |
| `local_infile` | 建议关闭（攻击面） |
| `secure_file_priv` | 建议非空 |
| `sql_mode` | 建议含严格模式 |
| `max_connections` | 复核异常区间 |
| `innodb_buffer_pool_size` | 复核占内存 50%~75% |
| `max_connect_errors` | 建议不要太小 |

## 文件结构

```
compare_my_cnf.py        # 主脚本（CLI、解析、对比、报告）
mysql_checker.py         # MySQL 连接、归一化、参数模型、加固巡检
requirements.txt         # 依赖（PyMySQL）
tests/test_tool.py       # 单元测试
```

## 运行测试

```bash
python3 -m unittest tests.test_tool -v
```

## 依赖

- Python 3.6+
- PyMySQL（`pip3 install PyMySQL`）

> **Python 3.6 用户注意**：PyMySQL 1.0.3+ 要求 Python ≥3.7，3.6 能装的最高版本是 `1.0.2`；
> 且 3.6 自带的 pip 较旧，直接 `pip install` 可能静默失败，需先升级 pip：
> ```bash
> python -m pip install --upgrade "pip==21.3.1"   # 最后一个支持 3.6 的 pip
> python -m pip install "PyMySQL==1.0.2"
> ```

## 注意事项

1. **重复 key**：同一 section 内重复 key，保留最后一个值（与 MySQL 行为一致）并警告。
2. **归一化**：`1G`/`1024M`/`1073741824` 视为相等；`on`/`1`/`yes`/`true` 归为 `ON`；路径忽略尾部斜杠；枚举字符串大小写不敏感。
3. **连字符等价**：`log-bin` 与 `log_bin` 视为同一参数。
4. **无值布尔**：`skip-name-resolve`、`log-bin` 等无 `=` 的行视为开启。
5. **白名单**：`server_id`、`port` 等在不同主机上必须不同，用 `--allow-diff` 忽略。
6. **只读变量**：`version`/`innodb_version`/`hostname` 等不可在 my.cnf 设置，对比时自动排除。
7. **模式选择**：`file-vs-running` 靠配置文件路径里的 IP 定位实例；路径不含 IP 或对比本地实例（如 `127.0.0.1`）时改用 `file-vs-running-host`，用 `--config-file` + `--mysql-host` 显式指定。`--mysql-host` 仅在后者生效，其他模式传了会被忽略并警告。
