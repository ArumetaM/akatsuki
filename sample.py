def notify_all():

    # 実行日
    dt_now = datetime.datetime.now()
    day = "{}{}{}".format(str(dt_now.year-2000).zfill(2),str(dt_now.month).zfill(2),str(dt_now.day).zfill(2))

    horses = pd.read_csv(get_path("production","day",day)+"buy.csv",encoding="cp932")
    horses["馬キー"] = horses["馬キー"].apply(lambda x:str(x).zfill(10))

    for i in range(len(horses)):
        key = horses["馬キー"][i]
        hour = horses["hour"][i]
        minute = horses["minute"][i]
        field = horses["競馬場"][i]
        race = horses["Race"][i]
        name = horses["horse_name"][i]
        number = horses["Number"][i]
        # power = horses["単勝期待値"][i]
        # information = "{}年{}月{}日{}時{}分\n競馬場:{}\nレース:{}\n馬名:{}\n馬番:{}\n単勝期待値:{}".format(dt_now.year,dt_now.month,dt_now.day,hour,minute,field,race,name,number,power)
        power = horses["単勝期待値"][i]
        information = "{}年{}月{}日{}時{}分\n競馬場:{}\nレース:{}\n馬名:{}\n馬番:{}\n単勝期待値:{}".format(dt_now.year,dt_now.month,dt_now.day,hour,minute,field,race,name,number,power)

        data = [key,dt_now.year,dt_now.month,dt_now.day,hour,minute,field,race,name,number,power]
        make_log(data,"line",information)
        make_log(data,"slack",information)
        make_log(data,"twitter",information)
        time.sleep(30)

def make_log(data,app,info):

    columns_list = ["key","year","month","day","hour","minute","field","race","name","number","power"]

    # logフォルダ
    if os.path.exists(get_path("production")+"log"):
        pass
    else:
        os.mkdir(get_path("production")+"log")

    # appフォルダ
    if os.path.exists(get_path("production","log")+app):
        pass
    else:
        os.mkdir(get_path("production","log")+app)

    if os.path.isfile(get_path("production","log",app)+"log.pic"):
        with open(get_path("production","log",app)+"log.pic","rb") as f:
            log = pickle.load(f)
        key_list = []
        for i in range(len(log)):
            key_list.append(log[i][0])
    else:
        log = []
        key_list = []

    if data[0] in key_list:
        print("既に{}には通知を出しています．".format(app))
    else:
        print("{}に通知を出します．".format(app))
        try:
            if app == "line":
                line_api(info)
            elif app == "twitter":
                tweet(info)
            elif app == "slack":
                slack_api(info,"馬-予測")
            log.append(data)
            with open(get_path("production","log",app)+"log.pic","wb") as f:
                pickle.dump(log,f)
            df = pd.DataFrame(log,columns=columns_list)
            df.to_csv(get_path("production","log",app)+"log.csv",encoding="cp932")
        except:
            print("Error")

def auto_program(field,race,number,name,odd_restrict):

    dt_now = datetime.datetime.now().weekday()
    weekday_list = ["月","火","水","木","金","土","日"]
    field_name = "{}（{}）".format(field,weekday_list[dt_now])

    driver = webdriver.Chrome(ChromeDriverManager().install())
    driver.maximize_window()

    # ログイン画面の表示
    driver.get("https://www.ipat.jra.go.jp/")
    time.sleep(4)
    # INET-IDの入力
    driver.find_element_by_name("inetid").send_keys("GRFWW8PA")
    # 次の画面への遷移
    driver.find_element_by_class_name("button").click()

    time.sleep(4)
    # 加入者番号の入力
    driver.find_element_by_name("i").send_keys("61008176")
    # 暗証番号の入力
    driver.find_element_by_name("p").send_keys("9262")
    # P-ARS番号の入力
    driver.find_element_by_name("r").send_keys("0519")
    # 次の画面への遷移
    driver.find_element_by_class_name("buttonModern").click()

    # お知らせなどの確認画面の判定(OKがあればOKをクリック)
    try:
        time.sleep(4)
        button_list = driver.find_elements_by_tag_name("button")
        for button in button_list:
            if "OK" in button.text:
                button.click()
    except:
        pass

    # 残金の取得
    time.sleep(4)
    button_list = driver.find_elements_by_tag_name("td")
    for button in button_list:
        if "円" in button.text:
            bet = int(button.text.replace(",","").replace("円",""))
            break

    bet = 100
    now_money = bet
    # bet = int(bet*3//10000)*100
    # if bet < 300:
    #     bet = 300
    # bet = min(bet,200000)


    # 投票画面への移動
    button_list = driver.find_elements_by_tag_name("button")
    for button in button_list:
        if "通常" in button.text and "投票" in button.text:
            button.click()
            break

    time.sleep(4)
    # 競馬場の選択(例:小倉（土）)
    button_list = driver.find_elements_by_tag_name("button")
    for button in button_list:
        if field_name in button.text:
            button.click()
            break
    # 何レースの選択(10R)
    button_list = driver.find_elements_by_tag_name("button")
    for button in button_list:
        if len("{}R".format(race)) == 2 and button.text[0:2] == "{}R".format(race):
            button.click()
            break
        elif len("{}R".format(race)) == 3 and button.text[0:3] == "{}R".format(race):
            button.click()
            break

    # 馬番から買う馬券を選択
    time.sleep(4)
    tag_list = driver.find_elements_by_tag_name("label")

    # スクロール
    if number >= 9:
        driver.execute_script("window.scrollTo(0, 300);")
        time.sleep(2)
        if number >= 13:
            driver.execute_script("window.scrollTo(0, 300);")
            time.sleep(2)

    cnt=0
    aaa_list = driver.find_elements_by_class_name("ng-isolate-scope")
    for i in range(len(aaa_list)):
        if name in aaa_list[i].text:
            bbb = aaa_list[i].text.split("\n")
            for j in bbb:
                if name in j:
                    target_odd = float(j.split(" ")[2])
                    break
            break

    if odd_restrict > target_odd:
        information = "競馬場:{}\n第{}レース\n馬番:{}\n馬名:{}\nオッズが{}のため購入しませんでした.".format(field,race,number,name,target_odd)
        line_api(information)
        # slack_info(information)
        driver.quit()
        return 0

    for a in tag_list:
        if cnt == number+8:
            a.click()
            break
        cnt+=1
    time.sleep(2)

    # セットのクリック
    button_list = driver.find_elements_by_tag_name("button")
    for button in button_list:
        if button.text == "セット":
            button.click()
            break

    time.sleep(2)
    # すべて入力を終えたら入力終了のクリック
    button_list = driver.find_elements_by_tag_name("button")
    for button in button_list:
        if button.text == "入力終了":
            button.click()
            break

    time.sleep(4)
    # 購入直前の投票票数の入力
    button = driver.find_elements_by_tag_name("input")
    button[9].send_keys(bet//100)
    time.sleep(1)
    # 賭け額の投票票数の入力
    button[10].send_keys(bet//100)
    time.sleep(1)
    # 最終合計金額の入力
    button[11].send_keys(bet)

    time.sleep(4)
    # 購入ボタン
    button_list = driver.find_elements_by_tag_name("button")
    for button in button_list:
        if button.text == "購入する":
            button.click()
            break
    time.sleep(4)
    button_list = driver.find_elements_by_tag_name("button")
    for button in button_list:
        if button.text == "OK":
            information = "競馬場:{}\n第{}レース\n馬番:{}\n馬名:{}\n掛け金:{}\nオッズ{}\n購入に成功しました.".format(field,race,number,name,money_fake(bet),target_odd)
            # information = "テスト、実際は購入していません。"+information
            button.click()
            line_api(information)
            # information = "競馬場:{}\n第{}レース\n馬番:{}\n馬名:{}\n掛け金:{}\n購入に成功しました.".format(field,race,number,name,bet)
            # now_money = money - bet
            # information += "\n現在の資金は{}．".format(now_money)
            # slack_info(information,"運用収益")

            break
    # 終了
    time.sleep(5)
    driver.quit()
    return bet

def update_money():

    driver = webdriver.Chrome(ChromeDriverManager().install())

    # ログイン画面の表示
    driver.get("https://www.ipat.jra.go.jp/")
    time.sleep(4)
    # INET-IDの入力
    driver.find_element_by_name("inetid").send_keys("GRFWW8PA")
    # 次の画面への遷移
    driver.find_element_by_class_name("button").click()

    time.sleep(4)
    # 加入者番号の入力
    driver.find_element_by_name("i").send_keys("61008176")
    # 暗証番号の入力
    driver.find_element_by_name("p").send_keys("9262")
    # P-ARS番号の入力
    driver.find_element_by_name("r").send_keys("0519")
    # 次の画面への遷移
    driver.find_element_by_class_name("buttonModern").click()

    # お知らせなどの確認画面の判定(OKがあればOKをクリック)
    try:
        time.sleep(4)
        button_list = driver.find_elements_by_tag_name("button")
        for button in button_list:
            if "OK" in button.text:
                button.click()
    except:
        pass

    # 残金の取得
    time.sleep(4)
    button_list = driver.find_elements_by_tag_name("td")
    for button in button_list:
        if "円" in button.text:
            bet = int(button.text.replace(",","").replace("円",""))
            break

    line_api("今日の最終残金は{}円です".format(money_fake(bet)))
    # slack_api("今日の最終残金は{}円です".format(bet),"本番環境")
    with open(get_path("data","money")+"now_money.pickle", mode='wb') as fp:
        pickle.dump(bet, fp)

    driver.quit()
    return bet

def deposit():
    with open(get_path("data","money")+"now_money.pickle", mode="rb") as fp:
        money = pickle.load(fp)

    driver = webdriver.Chrome(ChromeDriverManager().install())

    # ログイン画面の表示
    driver.get("https://www.ipat.jra.go.jp/")
    time.sleep(4)
    # INET-IDの入力
    driver.find_element_by_name("inetid").send_keys("GRFWW8PA")
    # 次の画面への遷移
    driver.find_element_by_class_name("button").click()

    time.sleep(4)
    # 加入者番号の入力
    driver.find_element_by_name("i").send_keys("61008176")
    # 暗証番号の入力
    driver.find_element_by_name("p").send_keys("9262")
    # P-ARS番号の入力
    driver.find_element_by_name("r").send_keys("0519")
    # 次の画面への遷移
    driver.find_element_by_class_name("buttonModern").click()

    # お知らせなどの確認画面の判定(OKがあればOKをクリック)
    try:
        time.sleep(4)
        button_list = driver.find_elements_by_tag_name("button")
        for button in button_list:
            if "OK" in button.text:
                button.click()
    except:
        pass

    # 残金の取得
    time.sleep(4)
    button_list = driver.find_elements_by_tag_name("td")
    for button in button_list:
        if "円" in button.text:
            bet = int(button.text.replace(",","").replace("円",""))
            break

    if bet != 0:
        driver.quit()
        return None
    else:
        # line_api("{}円入金します".format(money))
        line_api("{}円入金します".format(money_fake(money)))

        button_list = driver.find_elements_by_tag_name("button")
        for button in button_list:
            if "入出金" in button.text:
                button.click()
                break

        time.sleep(4)
        # 別ウィンドウへ移動
        handle_array = driver.window_handles
        driver.switch_to.window(handle_array[-1])
        button_list = driver.find_elements_by_tag_name("a")

        for button in button_list:
            if "入金指示" in button.text:
                button.click()
                break
        time.sleep(4)

        driver.find_element_by_name("NYUKIN").send_keys(str(money))

        button_list = driver.find_elements_by_tag_name("a")
        for button in button_list:
            if "次へ" in button.text:
                button.click()
        time.sleep(4)

        driver.find_element_by_name("PASS_WORD").send_keys("9262")
        button_list = driver.find_elements_by_tag_name("a")
        for button in button_list:
            if "実行" in button.text:
                button.click()
        time.sleep(4)

        Alert(driver).accept()

        time.sleep(4)

        driver.quit()

def define_money(add):
    with open(get_path("data","money")+"now_money.pickle", mode="rb") as fp:
        money = pickle.load(fp)
    new = money + add
    print("現在の所持金は{}円です．".format(money))
    print("{}円追加します．".format(add))
    print("次回は{}円からスタートします．".format(new))
    with open(get_path("data","money")+"now_money.pickle", mode='wb') as fp:
        pickle.dump(new, fp)

