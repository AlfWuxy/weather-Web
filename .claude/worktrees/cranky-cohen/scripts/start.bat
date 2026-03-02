@echo off
chcp 65001 >nul
set "ROOTDIR=%~dp0.."
pushd "%ROOTDIR%"
echo ====================================
echo 天气健康风险预测系统 - 启动脚本
echo ====================================
echo.

echo 正在检查Python环境...
python --version
if errorlevel 1 (
    echo [错误] 未找到Python，请先安装Python 3.10+
    pause
    exit /b 1
)
echo.

echo 正在检查依赖包...
python -m pip list | findstr Flask >nul
if errorlevel 1 (
    echo [提示] 检测到未安装依赖包，正在安装...
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [错误] 依赖包安装失败
        pause
        exit /b 1
    )
) else (
    echo [成功] 依赖包已安装
)
echo.

echo 正在检查数据库...
if not exist instance\\health_weather.db (
    echo [提示] 数据库不存在，正在导入数据...
    python services\\pipelines\\import_data.py
    if errorlevel 1 (
        echo [错误] 数据导入失败
        pause
        exit /b 1
    )
) else (
    echo [成功] 数据库已存在
)
echo.

echo ====================================
echo 启动Flask应用...
echo ====================================
echo.
echo 访问地址: http://localhost:5000
echo 管理员账号/密码请通过安全方式初始化（不要在脚本中明文展示）
echo.
echo 按Ctrl+C停止服务器
echo.

python app.py

pause
popd








