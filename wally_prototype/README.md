# Swift Appeal

我要製作一個訴願快速拆解平台，顏色主要是白色，次要顏色是深藍色（例如navbar)

訴願平台將會有五個步驟（畫面）拆解訴願，幫助法制局分析民眾訴願與原機關答辯，交付決定文草稿到委員會決定訴願結果



第一步驟：挑選承辦文件：訴願資格初判

畫面分成四個象限，訴願文件icon +案件名稱被分在四個象限中

第一象限：AI判讀高風險失格

第二象限：AI判讀過關

第三象限：人類確認過關

第四象限：人類確認失格

（文件可人工拖曳到不同象限）



每個案件可以點開，點開後視窗呈現原始文件，畫面一分為二，左邊為原始文件，一個可以畫重點的pdf檔案，右邊為AI判讀結果，AI分析文件為何失格/通關，從左邊圈出重點，並拉線到右邊寫明原因



文件判讀重點

原處分機關是誰？

案件分署？廢棄物清理法or洗錢防制法or空氣污染房防治法

送達程序：是否合於行政程序法送達規定

法定期間內提出：是或否

先行程序：是否已踐行（原機關是否已答辯）



畫面右下角：人工審核

是否同意AI判讀，如否就需要撰寫原因，並指示拖曳文件到對應象限，只有人類確認通關之象限可以進入下一步驟，人工判斷不通過的文件，點開後顯示「流程等待確認」

如同意，進入第二步，從第二步驟開始，畫面左上恆常出現訴願人＋案號



訴願與答辯書的事實與爭點整理

畫面一左一右，左邊呈現AI整理的訴願事實，右邊呈現AI整理答辯理由，兩者都可以點開閱讀原始文件

畫面上方顯示爭點關鍵句按鈕 ，當點擊爭點關鍵句（例如：爭議廢棄物是否為「棄置」還是「暫放」），顯示左右畫面中對應的語句



畫面下方出現兩個按鈕：人工新增爭點 、完成爭點整理，進入下一步



第三步驟 訴願決定與資料援引

說明文字：根據訴願事實與爭點整理，AI建議三個相關法條與三個相似案例。



左邊畫面上半部訴願文件的訴願事實，下半部是答辯理由

右邊畫面上半部是建議參考法條下半部是參考過去的案例（接要超連結可以點擊展開查看或新開分頁）



畫面下方出現兩個按鈕：人工新增參考資料、進入下一步，決定文草擬



第四步驟 決定文草擬

左右畫面

左邊畫面出現決定文文件草稿

格式需要：



案 號： 

要 旨： 

發文日期： 

發文字號： 

相關法條： 

全 文：

新北市政府訴願決定書 案號：

訴願人：

原處分機關：

上列訴願人因違反洗錢防制法事件，不服原處分機關民國 112 年 11 月 12 日書面

告誡（案件編號：1124430434）所為之處分，提起訴願一案，本府依法決定如下：

主 文



理 由



左邊畫面：AI判斷之依據



原始訴願書重點：（範例）

訴願人主張系爭建築剩餘土石方屬施工期間「暫置」性質，非長期棄置，不應認定違反廢棄物清理法。

答辯書重點：（範例）

惟依原處分機關現場稽查紀錄及空拍存證影像，系爭廢棄物放置已逾90日未曾清運，核與「棄置」之認定基準相符。



法條編輯：（要有超連結）

按廢棄物清理法第27條規定，於同一地點長期放置逾相當期間未清除者，即屬「棄置」，非屬暫置性質。

相似案例編輯（要有超連結）

查111年度0562號訴願決定同涉工地剩餘料暫置╱棄置認定爭議，惟該案稽查間隔僅30日，與本案基礎事實尚有差異，僅得部分援引其論理架構。

AI綜合判斷編輯（結論）

綜合上情，

左邊畫面可以編輯，右邊畫面可以新增參考依據



畫面右下角：將草稿存入庫存

網站參考資料：我會給你一個民眾的訴願書、原始機關的答辯書，他們是主要要往下走的依據資料

參考網站：只參考AI判斷內容，不參考畫面UI設計
https://claude.ai/code/artifact/bb47afe5-a063-4f4a-8896-0cf12cde41f2?open_in_browser=1&via=user_open&org=2ca13d33-bf19-4381-a46c-b7d70001f347

This project was built with [Lovable](https://lovable.dev).

**Live app**: https://appeal-streamliner.lovable.app

## Build with Lovable

Continue developing this project in the [Lovable editor](https://lovable.dev/projects/534d65d6-d749-43b9-89a7-3107f9a92375).

- **Ship faster**: describe what you want to build and Lovable handles the code.
- **Stay in sync**: every change made in Lovable is committed straight to this repository.
- **Full ownership**: this code is yours. Push to `main` on GitHub and your changes sync back into Lovable, ready for your next prompt.

## Development

Prefer working locally? You need Node.js and npm — [install with nvm](https://github.com/nvm-sh/nvm#installing-and-updating).

```sh
git clone <this-repository-url>
cd <repository-name>
npm i
npm run dev
```
