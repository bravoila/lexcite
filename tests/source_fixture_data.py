"""Synthetic source data; never import from production. No real legal conclusions."""
from app.research_agent import authorities

CRIMINAL=authorities.entry_for('刑法')
BASIS=CRIMINAL['revision']+'\n2023年12月29日第十四届全国人民代表大会常务委员会第七次会议通过\n八、本修正案自2024年3月1日起施行。'
CRIMINAL_TEXT=CRIMINAL['name']+'\n根据2023年12月29日通过的《'+CRIMINAL['revision']+'》修正\n'+'\n'.join(f'第{i}条 合成条文{i}仅供自动化测试，不是法律依据。'+('\n第17条之一 合成增加条文，测试拆条保留。' if i==17 else '') for i in range(1,453))+'\n本法自1997年10月1日起施行。'
